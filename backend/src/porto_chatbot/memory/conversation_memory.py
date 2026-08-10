"""ConversationMemory — vector repository operations with SQLite hydration.

Uses a :class:`VectorRepository` for indexing/search and :class:`SessionStore`
for canonical text hydration.  Dimension mismatches degrade gracefully without
auto-reset.
"""
from __future__ import annotations

from ..embeddings import EmbeddingClient
from ..logging_utils import get_component_logger
from ..models import MessageRecord, SourceChunk
from ..settings import Settings
from ..vector_stores import (
    ChromaVectorRepository,
    CollectionSpec,
    DimensionMismatchError,
    DistanceMetric,
    VectorRecord,
    VectorRepository,
    VectorSearchRequest,
)
from .session_store import SessionStore


class ConversationMemory:
    """Vector indexing and retrieval backed by a VectorRepository."""

    def __init__(
        self,
        settings: Settings,
        *,
        repo: VectorRepository | None = None,
        session_store: SessionStore | None = None,
    ):
        self.settings = settings
        self.logger = get_component_logger("conv_memory", settings)
        self._session_store = session_store or SessionStore(settings)
        self._repo = repo or ChromaVectorRepository(settings.chroma_dir)
        self._collection_name = settings.memory_collection
        self.embeddings = EmbeddingClient(settings)
        dim = self.embeddings.probe_dimension()
        self._spec = CollectionSpec(
            name=self._collection_name,
            dimension=dim,
            metric=DistanceMetric.L2,
            metadata={"kind": "memory"},
        )
        self._repo.ensure_collection(self._spec)
        self.logger.info(
            "conversation memory ready collection=%s dim=%s", self._collection_name, dim,
        )

    def index(self, records: list[MessageRecord]) -> None:
        """Batch embedding + repository upsert.

        Raises :class:`DimensionMismatchError` on dimension conflict — the
        caller decides the degradation strategy.  Never auto-resets.
        """
        if not records:
            return
        embeddings = self.embeddings.embed_documents([r.content for r in records])
        vector_records = [
            VectorRecord(
                id=r.id,
                embedding=list(emb),
                document=r.content,
                metadata={
                    "session_id": r.session_id,
                    "role": r.role,
                    "intent": r.intent or "",
                    "created_at": r.created_at,
                    "message_id": r.id,
                },
            )
            for r, emb in zip(records, embeddings, strict=False)
        ]
        self._repo.upsert(self._collection_name, vector_records)
        self.logger.info("memory indexed records=%s", len(records))

    def search(
        self, query: str, *, session_id: str, top_k: int = 5,
    ) -> list[SourceChunk]:
        """Session-isolated vector search with SQLite text hydration."""
        if self._repo.count(self._collection_name) == 0:
            return []
        query_embedding = self.embeddings.embed_query(query)
        try:
            hits = self._repo.search(
                self._collection_name,
                VectorSearchRequest(
                    query_embedding=query_embedding,
                    top_k=top_k,
                    where={"session_id": session_id},
                ),
            )
        except DimensionMismatchError:
            self.logger.warning(
                "memory dimension mismatch on search, degrading to empty",
            )
            return []

        if not hits:
            return []

        # Hydrate canonical text from SQLite by message ID.
        messages = self._session_store.get_messages_by_ids([h.id for h in hits])

        rows: list[SourceChunk] = []
        for hit in hits:
            msg = messages.get(hit.id)
            if msg is None:
                continue  # message deleted from SQLite — omit
            rows.append(
                SourceChunk(
                    id=hit.id,
                    path=f"memory:{msg.session_id}",
                    title=str(msg.role),
                    text=msg.content,
                    score=round(hit.score, 4),
                    metadata=dict(hit.metadata),
                )
            )
        self.logger.info(
            "memory search session=%s query_chars=%s results=%s",
            session_id, len(query), len(rows),
        )
        return rows

    def count(self, session_id: str | None = None) -> int:
        """Vector count, optionally filtered by session.

        Without a session, delegates to the repository.  With a session,
        counts indexed messages in SQLite for accuracy.
        """
        if session_id is None:
            return self._repo.count(self._collection_name)
        return self._session_store.count_indexed(session_id)

    def reset(self) -> None:
        """Clear all vectors (dimension change, etc.).  Note: erases vector memory."""
        if hasattr(self._repo, "drop_collection"):
            self._repo.drop_collection(self._collection_name)
        else:
            for batch in self._repo.export_batches(self._collection_name, 500):
                self._repo.delete(self._collection_name, [r.id for r in batch])
        self._repo.ensure_collection(self._spec)
        self.logger.info("memory collection reset done")
