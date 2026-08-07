"""ConversationMemory — 纯 ChromaDB 向量操作层。

只做 index/search/count/reset，不碰 SQLite。session_id 在 search 中必填。
"""
from __future__ import annotations

import chromadb

from ..embeddings import EmbeddingClient
from ..logging_utils import get_component_logger
from ..models import MessageRecord, SourceChunk
from ..settings import Settings


class ConversationMemory:
    """ChromaDB 层：会话向量的索引与检索。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("conv_memory", settings)
        self.settings.chroma_dir.mkdir(parents=True, exist_ok=True)
        self.embeddings = EmbeddingClient(settings)
        self.client = chromadb.PersistentClient(path=str(settings.chroma_dir))
        self.collection = self.client.get_or_create_collection(settings.memory_collection)
        self.logger.info(
            "conversation memory ready collection=%s", settings.memory_collection,
        )

    def index(self, records: list[MessageRecord]) -> None:
        """批量 embedding + 写入 ChromaDB。metadata 含 session_id/role/intent/created_at/message_id。

        失败时抛异常（维度不匹配则自动 reset 重试），由调用方决定降级策略。
        """
        if not records:
            return
        embeddings = self.embeddings.embed_documents([r.content for r in records])
        ids = [r.id for r in records]
        documents = [r.content for r in records]
        metadatas = [
            {
                "session_id": r.session_id,
                "role": r.role,
                "intent": r.intent or "",
                "created_at": r.created_at,
                "message_id": r.id,
            }
            for r in records
        ]
        try:
            self.collection.add(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
                embeddings=embeddings,
            )
        except Exception as exc:
            if "dimension" not in str(exc).lower():
                raise
            self.logger.warning("memory collection dim mismatch on index, rebuilding: %s", exc)
            self.reset()
            self.collection.add(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
                embeddings=embeddings,
            )
        self.logger.info("memory indexed records=%s", len(records))

    def search(
        self, query: str, *, session_id: str, top_k: int = 5,
    ) -> list[SourceChunk]:
        """session 隔离的向量检索。session_id 必填。"""
        if self.collection.count() == 0:
            return []
        query_embedding = self.embeddings.embed_query(query)
        try:
            result = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                where={"session_id": session_id},
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            if "dimension" not in str(exc).lower():
                raise
            self.logger.warning("memory collection dim mismatch on search, rebuilding: %s", exc)
            self.reset()
            return []
        rows: list[SourceChunk] = []
        for item_id, doc, metadata, distance in zip(
            result.get("ids", [[]])[0],
            result.get("documents", [[]])[0],
            result.get("metadatas", [[]])[0],
            result.get("distances", [[]])[0],
            strict=False,
        ):
            rows.append(
                SourceChunk(
                    id=item_id,
                    path=f"memory:{metadata.get('session_id', '')}",
                    title=str(metadata.get("role", "memory")),
                    text=doc or "",
                    score=round(1.0 / (1.0 + max(0.0, float(distance))), 4),
                    metadata=dict(metadata),
                )
            )
        self.logger.info(
            "memory search session=%s query_chars=%s results=%s",
            session_id, len(query), len(rows),
        )
        return rows

    def count(self, session_id: str | None = None) -> int:
        """向量数。可选按 session 过滤。"""
        if session_id is None:
            return self.collection.count()
        # ChromaDB count with where filter
        result = self.collection.get(where={"session_id": session_id})
        return len(result.get("ids", []))

    def reset(self) -> None:
        """重建 collection（embedding 维度变化等场景）。注意：会清空向量记忆。"""
        try:
            self.client.delete_collection(self.settings.memory_collection)
        except Exception:
            self.logger.info("memory collection reset skipped (not existed)")
        self.collection = self.client.get_or_create_collection(self.settings.memory_collection)
        self.logger.info("memory collection reset done")
