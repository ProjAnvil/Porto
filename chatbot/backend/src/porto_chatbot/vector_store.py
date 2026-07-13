from __future__ import annotations

import hashlib
import math
import shutil
import threading
from typing import Any, Callable

import chromadb

from .documents import (
    SPLITTER_VERSION,
    chunk_document,
    detect_content_format,
    iter_documents,
    read_document,
)
from .embeddings import EmbeddingClient
from .logging_utils import get_component_logger
from .models import IndexStats, SourceChunk
from .settings import Settings

COLLECTION_METADATA_KEYS = {
    "embedding_provider",
    "embedding_model",
    "embedding_base_url",
    "chunk_size",
    "chunk_overlap",
    "splitter",
}


def cosine(a: list[float], b: list[float]) -> float:
    numerator = sum(x * y for x, y in zip(a, b, strict=False))
    a_norm = math.sqrt(sum(x * x for x in a))
    b_norm = math.sqrt(sum(y * y for y in b))
    if a_norm == 0 or b_norm == 0:
        return 0.0
    return numerator / (a_norm * b_norm)


class ChromaVectorStore:
    _build_lock = threading.Lock()

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("vector_store", settings)
        self.settings.chroma_dir.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(self.settings.chroma_dir))
        self.embeddings = EmbeddingClient(settings)
        self.logger.info(
            "vector store ready backend=chroma collection=%s chroma_dir=%s",
            self.settings.vector_collection,
            self.settings.chroma_dir,
        )

    def build(
        self,
        reset: bool = True,
        progress_cb: Callable[[int, int, int], None] | None = None,
    ) -> IndexStats:
        with self._build_lock:
            return self._build_impl(reset, progress_cb)

    def _build_impl(
        self,
        reset: bool = True,
        progress_cb: Callable[[int, int, int], None] | None = None,
    ) -> IndexStats:
        self.logger.info(
            "index build start kb_path=%s reset=%s provider=%s model=%s chunk_size=%s overlap=%s",
            self.settings.kb_path,
            reset,
            self.settings.embedding_provider,
            self.settings.embedding_model,
            self.settings.max_chunk_chars,
            self.settings.chunk_overlap,
        )
        if reset:
            self._reset_collection(self.settings.vector_collection)
        collection = self.client.get_or_create_collection(
            self.settings.vector_collection,
            metadata=self._expected_collection_metadata(),
        )

        documents = iter_documents(self.settings.kb_dirs)
        total = len(documents)
        self.logger.info("index documents discovered count=%s", total)
        chunk_count = 0
        batch_ids: list[str] = []
        batch_texts: list[str] = []
        batch_metadata: list[dict[str, Any]] = []

        for idx, (root, path) in enumerate(documents):
            if progress_cb is not None:
                progress_cb(idx + 1, total, chunk_count)
            try:
                text = read_document(path)
            except Exception:
                self.logger.exception("document read failed path=%s", path)
                continue
            rel = str(path.relative_to(root))
            root_name = root.name or "root"
            display_path = f"{root_name}/{rel}"
            content_format = detect_content_format(path)
            chunks = chunk_document(
                text,
                content_format=content_format,
                max_chars=self.settings.max_chunk_chars,
                overlap=self.settings.chunk_overlap,
            )
            for i, chunk in enumerate(chunks):
                batch_ids.append(hashlib.sha1(f"{display_path}:{i}:{chunk.text[:120]}".encode()).hexdigest())
                batch_texts.append(chunk.text)
                metadata = {
                    "path": display_path,
                    "title": path.stem,
                    "root": root_name,
                    "chunk": i,
                    "source_mtime": path.stat().st_mtime,
                }
                metadata.update(chunk.metadata)
                batch_metadata.append(metadata)
                chunk_count += 1
                if len(batch_ids) >= 64:
                    self._add_batch(collection, batch_ids, batch_texts, batch_metadata)
                    self.logger.info("index batch added size=64")
                    batch_ids, batch_texts, batch_metadata = [], [], []

        if batch_ids:
            self._add_batch(collection, batch_ids, batch_texts, batch_metadata)
            self.logger.info("index batch added size=%s", len(batch_ids))

        if progress_cb is not None:
            progress_cb(total, total, chunk_count)

        stats = IndexStats(
            kb_path=str(self.settings.kb_path),
            documents=len(documents),
            chunks=chunk_count,
            backend="chroma",
            embedding_provider=self.settings.embedding_provider,
            embedding_model=self.settings.embedding_model,
            embedding_dimensions=self._collection_embedding_dimensions(collection),
            chunk_size=self.settings.max_chunk_chars,
            chunk_overlap=self.settings.chunk_overlap,
        )
        self.logger.info("index build finish documents=%s chunks=%s", stats.documents, stats.chunks)
        return stats

    def stats(self) -> IndexStats:
        collection = self._compatible_collection()
        stats = IndexStats(
            kb_path=str(self.settings.kb_path),
            documents=len(iter_documents(self.settings.kb_dirs)),
            chunks=collection.count(),
            backend="chroma",
            embedding_provider=self.settings.embedding_provider,
            embedding_model=self.settings.embedding_model,
            embedding_dimensions=self._collection_embedding_dimensions(collection),
            chunk_size=self.settings.max_chunk_chars,
            chunk_overlap=self.settings.chunk_overlap,
        )
        self.logger.info("index stats documents=%s chunks=%s", stats.documents, stats.chunks)
        return stats

    def search(self, query: str, top_k: int | None = None) -> list[SourceChunk]:
        collection = self._compatible_collection()
        if not self._is_collection_compatible(collection) or collection.count() == 0:
            self.logger.info("search skipped index unavailable query_chars=%s", len(query))
            return []
        resolved_top_k = top_k or self.settings.top_k
        self.logger.info("search start query_chars=%s top_k=%s", len(query), resolved_top_k)
        query_embedding = self.embeddings.embed_query(query)
        stored_dimensions = self._collection_embedding_dimensions(collection)
        if stored_dimensions is not None and stored_dimensions != len(query_embedding):
            self.logger.warning(
                "embedding dimension mismatch detected stored=%s current=%s action=skip",
                stored_dimensions,
                len(query_embedding),
            )
            return []
        result = collection.query(
            query_embeddings=[query_embedding],
            n_results=resolved_top_k,
            include=["documents", "metadatas", "distances"],
        )
        rows: list[SourceChunk] = []
        ids = result.get("ids", [[]])[0]
        docs = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        for item_id, doc, metadata, distance in zip(ids, docs, metadatas, distances, strict=False):
            score = 1.0 / (1.0 + max(0.0, float(distance)))
            rows.append(
                SourceChunk(
                    id=item_id,
                    path=str(metadata.get("path", "")),
                    title=str(metadata.get("title", "")),
                    text=doc or "",
                    score=round(score, 4),
                    metadata=dict(metadata),
                )
            )
        self.logger.info("search finish results=%s", len(rows))
        return rows

    def ensure_index(self) -> IndexStats:
        """只读：返回当前索引统计；**不触发 build**。重建由 IndexSupervisor 统一调度。"""
        collection = self._compatible_collection()
        if collection.count() == 0:
            self.logger.info("ensure index empty (no build triggered)")
        else:
            self.logger.info("ensure index existing count=%s", collection.count())
        return self.stats()

    def _add_batch(self, collection, ids: list[str], texts: list[str], metadata: list[dict[str, Any]]):
        self.logger.info("embedding and adding batch size=%s", len(ids))
        embeddings = self.embeddings.embed_documents(texts)
        if embeddings:
            dimensions = len(embeddings[0])
            self._set_collection_metadata(collection, embedding_dimensions=dimensions)
        collection.add(
            ids=ids,
            documents=texts,
            metadatas=metadata,
            embeddings=embeddings,
        )

    def _reset_collection(self, name: str) -> None:
        try:
            self.client.delete_collection(name)
            self.logger.info("collection reset name=%s", name)
        except Exception:
            self.logger.info("collection reset skipped name=%s", name)
            pass

    def _compatible_collection(self):
        """获取当前 collection（**不重建**）。重建由 IndexSupervisor 统一调度；
        search/stats 路径不再触发同步 build，从根上消除并发互删。"""
        return self.client.get_or_create_collection(
            self.settings.vector_collection,
            metadata=self._expected_collection_metadata(),
        )

    def is_rag_ready(self) -> bool:
        """索引是否可用于检索：collection 存在、元数据兼容、且非空。"""
        try:
            collection = self._compatible_collection()
        except Exception:
            return False
        return self._is_collection_compatible(collection) and collection.count() > 0

    def _is_collection_compatible(self, collection) -> bool:
        metadata = collection.metadata or {}
        expected = self._expected_collection_metadata()
        for key in COLLECTION_METADATA_KEYS:
            if metadata.get(key) != expected.get(key):
                return False
        if metadata.get("embedding_dimensions") is None:
            # collection 新建、build 进行中（维度尚未写入）。视为兼容，避免并发的
            # search 把正被 build 的 collection 当不兼容删掉，导致 build _add_batch NotFound。
            return True
        if self.settings.embedding_provider == "local":
            return metadata.get("embedding_dimensions") == self.settings.embedding_dimensions
        return True

    def _expected_collection_metadata(self) -> dict[str, str | int]:
        metadata: dict[str, str | int] = {
            "embedding_provider": self.settings.embedding_provider,
            "embedding_model": self.settings.embedding_model,
            "embedding_base_url": self.settings.embedding_base_url,
            "chunk_size": self.settings.max_chunk_chars,
            "chunk_overlap": self.settings.chunk_overlap,
            "splitter": SPLITTER_VERSION,
        }
        if self.settings.embedding_provider == "local":
            metadata["embedding_dimensions"] = self.settings.embedding_dimensions
        return metadata

    def _set_collection_metadata(self, collection, *, embedding_dimensions: int) -> None:
        metadata = {
            **self._expected_collection_metadata(),
            "embedding_dimensions": embedding_dimensions,
        }
        if collection.metadata == metadata:
            return
        collection.modify(metadata=metadata)
        self.logger.info("collection metadata updated metadata=%s", metadata)

    def _collection_embedding_dimensions(self, collection) -> int | None:
        value = (collection.metadata or {}).get("embedding_dimensions")
        return int(value) if value is not None else None


class LocalVectorStore(ChromaVectorStore):
    """Compatibility alias; the implementation is now Chroma-backed."""


def clear_vector_data(settings: Settings) -> None:
    if settings.chroma_dir.exists():
        shutil.rmtree(settings.chroma_dir)
        get_component_logger("vector_store", settings).info("vector data cleared dir=%s", settings.chroma_dir)
