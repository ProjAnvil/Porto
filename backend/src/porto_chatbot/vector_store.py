from __future__ import annotations

import hashlib
import math
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .bm25_index import Bm25Index, Bm25Registry, ChunkRecord
from .documents import (
    SPLITTER_VERSION,
    chunk_document,
    detect_content_format,
    iter_documents,
    read_document,
)
from .embeddings import EmbeddingClient
from .kb_generation_store import KbGenerationStore
from .logging_utils import get_component_logger
from .models import IndexStats, SourceChunk
from .models.enums import EmbeddingProvider, RetrievalMethod
from .retrieval import hybrid_fusion_search, rerank_chunks
from .settings import Settings
from .vector_stores import (
    ChromaVectorRepository,
    CollectionSpec,
    DistanceMetric,
    VectorRecord,
    VectorRepository,
    VectorSearchRequest,
)

_CHUNK_ID_PREVIEW_LEN = 120
_EMBED_BATCH_SIZE = 64


def cosine(a: list[float], b: list[float]) -> float:
    numerator = sum(x * y for x, y in zip(a, b, strict=False))
    a_norm = math.sqrt(sum(x * x for x in a))
    b_norm = math.sqrt(sum(y * y for y in b))
    if a_norm == 0 or b_norm == 0:
        return 0.0
    return numerator / (a_norm * b_norm)


class ChromaVectorStore:
    _build_lock = threading.Lock()

    def __init__(self, settings: Settings, *, repo: VectorRepository | None = None):
        self.settings = settings
        self.logger = get_component_logger("vector_store", settings)
        self.settings.chroma_dir.mkdir(parents=True, exist_ok=True)
        self._repo = repo or ChromaVectorRepository(self.settings.chroma_dir)
        self._gen_store = KbGenerationStore(settings)
        self._gen_store.bootstrap()
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
        *,
        target_collection: str | None = None,
        target_bm25_path: Path | None = None,
    ) -> IndexStats:
        with self._build_lock:
            return self._build_impl(
                reset, progress_cb, target_collection, target_bm25_path,
            )

    def _build_impl(
        self,
        reset: bool = True,
        progress_cb: Callable[[int, int, int], None] | None = None,
        target_collection: str | None = None,
        target_bm25_path: Path | None = None,
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
        collection_name = target_collection or self.settings.vector_collection
        if target_collection is not None or reset:
            self._reset_collection(collection_name)
        self._ensure_collection(collection_name)

        documents = iter_documents(self.settings.kb_dirs)
        total = len(documents)
        self.logger.info("index documents discovered count=%s", total)
        chunk_count = 0
        all_chunks: list[ChunkRecord] = []
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
                chunk_id = hashlib.sha1(
                    f"{display_path}:{i}:{chunk.text[:_CHUNK_ID_PREVIEW_LEN]}".encode()
                ).hexdigest()
                batch_ids.append(chunk_id)
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
                all_chunks.append(ChunkRecord(chunk_id, chunk.text, metadata))
                chunk_count += 1
                if len(batch_ids) >= _EMBED_BATCH_SIZE:
                    self._add_batch(collection_name, batch_ids, batch_texts, batch_metadata)
                    self.logger.info("index batch added size=64")
                    batch_ids, batch_texts, batch_metadata = [], [], []

        if batch_ids:
            self._add_batch(collection_name, batch_ids, batch_texts, batch_metadata)
            self.logger.info("index batch added size=%s", len(batch_ids))

        if progress_cb is not None:
            progress_cb(total, total, chunk_count)

        # BM25 与 chroma 同源同序同生命周期：build 结束即构建并落盘
        if target_bm25_path is not None:
            Bm25Registry.build_for_path(
                collection_name, target_bm25_path, all_chunks,
            )
        else:
            Bm25Registry.build_and_save(self.settings, all_chunks)

        stats = IndexStats(
            kb_path=str(self.settings.kb_path),
            documents=len(documents),
            chunks=chunk_count,
            backend="chroma",
            embedding_provider=self.settings.embedding_provider,
            embedding_model=self.settings.embedding_model,
            embedding_dimensions=self._safe_dimension(collection_name),
            chunk_size=self.settings.max_chunk_chars,
            chunk_overlap=self.settings.chunk_overlap,
        )
        self.logger.info("index build finish documents=%s chunks=%s", stats.documents, stats.chunks)
        return stats

    def stats(self) -> IndexStats:
        name = self._active_collection_name()
        try:
            dims = self._repo.collection_info(name).dimension
        except Exception:
            dims = None
        stats = IndexStats(
            kb_path=str(self.settings.kb_path),
            documents=len(iter_documents(self.settings.kb_dirs)),
            chunks=self._safe_count(name),
            backend="chroma",
            embedding_provider=self.settings.embedding_provider,
            embedding_model=self.settings.embedding_model,
            embedding_dimensions=dims,
            chunk_size=self.settings.max_chunk_chars,
            chunk_overlap=self.settings.chunk_overlap,
        )
        self.logger.info("index stats documents=%s chunks=%s", stats.documents, stats.chunks)
        return stats

    def _search_raw(self, query: str, top_k: int) -> list[SourceChunk]:
        """基础检索（vector/bm25/hybrid），不含 rerank。供 query_transform 编排复用。"""
        collection_name = self._active_collection_name()
        try:
            info = self._repo.collection_info(collection_name)
        except Exception:
            self.logger.info("search skipped collection_info failed query_chars=%s", len(query))
            return []
        if info.count == 0:
            self.logger.info("search skipped empty collection query_chars=%s", len(query))
            return []
        query_embedding = self.embeddings.embed_query(query)
        if info.dimension is not None and info.dimension != len(query_embedding):
            self.logger.warning(
                "embedding dimension mismatch stored=%s query=%s action=skip",
                info.dimension, len(query_embedding),
            )
            return []
        method = self.settings.retrieval_method
        if method == RetrievalMethod.VECTOR:
            return self._vector_search(collection_name, query_embedding, top_k)
        if method == RetrievalMethod.BM25:
            return self._bm25_search(collection_name, query, top_k)
        return self._hybrid_search(collection_name, query_embedding, query, top_k)

    def search(self, query: str, top_k: int | None = None) -> list[SourceChunk]:
        resolved_top_k = top_k or self.settings.top_k
        method = self.settings.retrieval_method
        self.logger.info(
            "search start query_chars=%s top_k=%s method=%s", len(query), resolved_top_k, method
        )
        rows = self._search_raw(query, resolved_top_k)
        if self.settings.rerank_enabled and rows:
            rows = rerank_chunks(rows, query, self.settings)
        self.logger.info("search finish method=%s results=%s", method, len(rows))
        return rows

    def _to_source(self, cid: str, text: str, metadata: dict, score: float) -> SourceChunk:
        return SourceChunk(
            id=cid,
            path=str(metadata.get("path", "")),
            title=str(metadata.get("title", "")),
            text=text,
            score=round(score, 4),
            metadata=metadata,
        )

    # ── generation-aware collection / BM25 resolution ──

    def _active_collection_name(self) -> str:
        gen = self._gen_store.current()
        return gen.collection_name if gen else self.settings.vector_collection

    def _active_bm25(self) -> Bm25Index | None:
        gen = self._gen_store.current()
        if gen is not None:
            return Bm25Registry.get_for_path(
                gen.collection_name, Path(gen.bm25_path),
            )
        return Bm25Registry.get(self.settings)

    def _vector_search(
        self, collection_name: str, query_embedding: list[float], top_k: int,
    ) -> list[SourceChunk]:
        hits = self._repo.search(
            collection_name,
            VectorSearchRequest(query_embedding=query_embedding, top_k=top_k),
        )
        return [
            self._to_source(h.id, h.document, h.metadata, h.score) for h in hits
        ]

    def _bm25_search(self, collection_name: str, query: str, top_k: int) -> list[SourceChunk]:
        bm = self._active_bm25()
        if bm is None:
            self.logger.info("bm25 search skipped index absent")
            return []
        candidates = bm.query(query, top_k)  # [(chroma_id, metadata, score)]
        if not candidates:
            return []
        records = self._repo.fetch(collection_name, [c[0] for c in candidates])
        record_map = {r.id: r for r in records}
        rows: list[SourceChunk] = []
        for cid, meta, _score in candidates:
            record = record_map.get(cid)
            if record is not None:
                rows.append(self._to_source(cid, record.document, record.metadata, 0.0))
            else:
                rows.append(self._to_source(cid, "", meta, 0.0))
        return rows

    def _hybrid_search(
        self, collection_name: str, query_embedding, query: str, top_k: int,
    ) -> list[SourceChunk]:
        bm = self._active_bm25()
        nodes = hybrid_fusion_search(
            repo=self._repo,
            collection=collection_name,
            query_embedding=query_embedding,
            query_text=query,
            bm25_index=bm,
            top_k=top_k,
            candidate_k=self.settings.bm25_top_k,
            vector_weight=self.settings.hybrid_vector_weight,
        )
        return [
            self._to_source(
                n.node.id_, n.node.text, dict(n.node.metadata or {}), float(n.score or 0.0)
            )
            for n in nodes
        ]

    def ensure_index(self) -> IndexStats:
        """只读：返回当前索引统计；**不触发 build**。重建由 IndexSupervisor 统一调度。"""
        name = self._active_collection_name()
        count = self._safe_count(name)
        if count == 0:
            self.logger.info("ensure index empty (no build triggered)")
        else:
            self.logger.info("ensure index existing count=%s", count)
        return self.stats()

    def _add_batch(
        self, collection_name: str, ids: list[str], texts: list[str], metadata: list[dict[str, Any]],
    ):
        self.logger.info("embedding and adding batch size=%s", len(ids))
        embeddings = self.embeddings.embed_documents(texts)
        if embeddings:
            dimensions = len(embeddings[0])
            self._set_collection_metadata(collection_name, embedding_dimensions=dimensions)
        records = [
            VectorRecord(id=rid, embedding=list(emb), document=text, metadata=dict(meta))
            for rid, emb, text, meta in zip(ids, embeddings, texts, metadata, strict=False)
        ]
        self._repo.upsert(collection_name, records)

    def _reset_collection(self, name: str) -> None:
        if hasattr(self._repo, "drop_collection"):
            self._repo.drop_collection(name)
            self.logger.info("collection reset name=%s", name)

    def _ensure_collection(self, name: str) -> None:
        """Create the collection with KB metadata if it doesn't exist."""
        self._repo.ensure_collection(
            CollectionSpec(
                name=name,
                dimension=self.settings.embedding_dimensions,
                metric=DistanceMetric.L2,
                metadata=self._expected_collection_metadata(),
            )
        )

    def _safe_count(self, collection_name: str) -> int:
        """Count with TOCTOU tolerance for concurrent reindex resets."""
        try:
            return self._repo.count(collection_name)
        except Exception:
            self.logger.info("collection count failed (likely concurrent reset) -> 0")
            return 0

    def _safe_dimension(self, collection_name: str) -> int | None:
        """Best-effort dimension read; None if unavailable."""
        try:
            return self._repo.collection_info(collection_name).dimension
        except Exception:
            return None

    def is_rag_ready(self) -> bool:
        """索引是否可用于检索：collection 非空且维度匹配当前 embedding 配置。"""
        name = self._active_collection_name()
        try:
            info = self._repo.collection_info(name)
        except Exception:
            return False
        if info.count == 0:
            return False
        if info.dimension is not None and info.dimension != self.settings.embedding_dimensions:
            return False
        if self.settings.retrieval_method in (RetrievalMethod.BM25, RetrievalMethod.HYBRID):
            bm = self._active_bm25()
            if bm is None or len(bm) != info.count:
                return False
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
        if self.settings.embedding_provider == EmbeddingProvider.LOCAL:
            metadata["embedding_dimensions"] = self.settings.embedding_dimensions
        return metadata

    def _set_collection_metadata(self, collection_name: str, *, embedding_dimensions: int) -> None:
        if hasattr(self._repo, "update_metadata"):
            self._repo.update_metadata(
                collection_name,
                {**self._expected_collection_metadata(), "embedding_dimensions": embedding_dimensions},
            )
            self.logger.info("collection metadata updated dim=%s", embedding_dimensions)


class LocalVectorStore(ChromaVectorStore):
    """Compatibility alias; the implementation is now Chroma-backed."""


def clear_vector_data(settings: Settings) -> None:
    if settings.chroma_dir.exists():
        shutil.rmtree(settings.chroma_dir)
        get_component_logger("vector_store", settings).info(
            "vector data cleared dir=%s", settings.chroma_dir
        )
