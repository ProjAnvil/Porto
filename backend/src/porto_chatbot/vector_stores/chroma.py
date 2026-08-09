"""ChromaDB adapter implementing the :class:`VectorRepository` protocol.

All raw ``chromadb`` usage lives here (and only here).  Higher layers interact
exclusively through canonical records and the protocol defined in ``base.py``.
"""
from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import chromadb

from ..logging_utils import get_component_logger
from .base import (
    CollectionInfo,
    CollectionNotReadyError,
    CollectionSpec,
    DimensionMismatchError,
    DistanceMetric,
    VectorRecord,
    VectorSearchHit,
    VectorSearchRequest,
    VectorStoreConnectionError,
    VectorStoreHealth,
)

logger = get_component_logger("vector_stores.chroma")

_CHROMA_SPACE: dict[DistanceMetric, str] = {
    DistanceMetric.L2: "l2",
    DistanceMetric.COSINE: "cosine",
}

# ChromaDB 1.x rejects empty metadata dicts; records with no user metadata get
# this placeholder on write and are cleaned back to {} on read.
_PLACEHOLDER_KEY = "_chroma_ph"


def _clean_metadata(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Strip the internal placeholder so callers see their original metadata."""
    if not meta:
        return {}
    cleaned = dict(meta)
    if cleaned.get(_PLACEHOLDER_KEY) is True:
        cleaned.pop(_PLACEHOLDER_KEY, None)
    return cleaned


def _normalize_score(distance: float, metric: DistanceMetric) -> float:
    """Convert a raw backend distance into a higher-is-better score.

    * **L2** — ``exp(-distance)``; matches the formula already used by
      LlamaIndex's Chroma integration, preserving existing KB ranking.
    * **Cosine** — Chroma returns ``1 − cosine_similarity`` in ``[0, 2]``;
      map to ``[0, 1]`` so that *higher* always means *more similar*.
    """
    if metric == DistanceMetric.L2:
        return math.exp(-distance)
    return max(0.0, 1.0 - distance / 2.0)


class ChromaVectorRepository:
    """ChromaDB-backed implementation of :class:`VectorRepository`."""

    def __init__(self, chroma_path: Path | str):
        self._path = Path(chroma_path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self._path))
        # Specs registered via ensure_collection — used for dimension checks
        # and metric-aware score normalisation.
        self._specs: dict[str, CollectionSpec] = {}

    # ── internal helpers ──

    def _get_collection(self, name: str):
        """Return the Chroma collection, mapping errors to typed exceptions."""
        try:
            return self._client.get_collection(name)
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "does not exist" in msg or "not exist" in msg:
                raise CollectionNotReadyError(
                    f"Collection {name!r} not found",
                ) from exc
            raise VectorStoreConnectionError(
                f"Failed to access collection {name!r}",
            ) from exc

    def _check_dimension(
        self, collection: str, records: Sequence[VectorRecord],
    ) -> None:
        spec = self._specs.get(collection)
        if spec is None:
            return
        for record in records:
            if len(record.embedding) != spec.dimension:
                raise DimensionMismatchError(
                    f"Collection {collection!r} expects dimension {spec.dimension}, "
                    f"record {record.id!r} has {len(record.embedding)}",
                )

    @property
    def _metric_for(self) -> dict[str, DistanceMetric]:
        return {name: spec.metric for name, spec in self._specs.items()}

    # ── protocol implementation ──

    def ensure_collection(self, spec: CollectionSpec) -> None:
        self._specs[spec.name] = spec
        metadata: dict[str, Any] = {
            **spec.metadata,
            "hnsw:space": _CHROMA_SPACE[spec.metric],
            "dimension": spec.dimension,
        }
        self._client.get_or_create_collection(name=spec.name, metadata=metadata)
        logger.info(
            "collection ensured name=%s metric=%s dim=%s",
            spec.name, spec.metric, spec.dimension,
        )

    def upsert(self, collection: str, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        self._check_dimension(collection, records)
        col = self._get_collection(collection)
        metadatas = [
            r.metadata if r.metadata else {_PLACEHOLDER_KEY: True}
            for r in records
        ]
        try:
            col.upsert(
                ids=[r.id for r in records],
                embeddings=[r.embedding for r in records],
                documents=[r.document for r in records],
                metadatas=metadatas,
            )
        except Exception as exc:
            if "dimension" in str(exc).lower():
                raise DimensionMismatchError(str(exc)) from exc
            raise

    def search(
        self, collection: str, request: VectorSearchRequest,
    ) -> list[VectorSearchHit]:
        col = self._get_collection(collection)
        if col.count() == 0:
            return []
        query_kwargs: dict[str, Any] = {
            "query_embeddings": [request.query_embedding],
            "n_results": request.top_k,
            "include": ["documents", "metadatas", "distances"],
        }
        if request.where:
            query_kwargs["where"] = request.where
        try:
            result = col.query(**query_kwargs)
        except Exception as exc:
            if "dimension" in str(exc).lower():
                raise DimensionMismatchError(str(exc)) from exc
            raise

        metric = self._metric_for.get(collection, DistanceMetric.L2)
        ids = result.get("ids", [[]])[0]
        docs = result.get("documents", [[]])[0]
        metas = result.get("metadatas", [[]])[0]
        dists = result.get("distances", [[]])[0]
        return [
            VectorSearchHit(
                id=id_,
                document=doc or "",
                metadata=_clean_metadata(meta),
                score=_normalize_score(float(dist), metric),
            )
            for id_, doc, meta, dist in zip(ids, docs, metas, dists, strict=False)
        ]

    def fetch(self, collection: str, ids: Sequence[str]) -> list[VectorRecord]:
        if not ids:
            return []
        col = self._get_collection(collection)
        fetched = col.get(
            ids=list(ids),
            include=["embeddings", "documents", "metadatas"],
        )
        f_ids = fetched.get("ids", [])
        f_embs = fetched.get("embeddings", [])
        f_docs = fetched.get("documents", [])
        f_metas = fetched.get("metadatas", [])
        records: list[VectorRecord] = []
        for i, rid in enumerate(f_ids):
            emb = f_embs[i] if i < len(f_embs) else []
            doc = f_docs[i] if i < len(f_docs) else ""
            meta = f_metas[i] if i < len(f_metas) else {}
            records.append(
                VectorRecord(
                    id=rid,
                    embedding=list(emb) if emb is not None else [],
                    document=doc or "",
                    metadata=_clean_metadata(meta),
                )
            )
        return records

    def export_batches(
        self, collection: str, batch_size: int,
    ) -> Iterator[list[VectorRecord]]:
        """Yield primary-key-ascending batches bounded by an ID snapshot.

        Records inserted *after* iteration begins do not appear in the
        traversal, satisfying the snapshot contract required by migration.
        """
        col = self._get_collection(collection)
        # Capture the full ID list once — new inserts won't appear.
        snapshot = sorted(col.get(include=[]).get("ids", []))
        for i in range(0, len(snapshot), batch_size):
            yield self.fetch(collection, snapshot[i : i + batch_size])

    def count(self, collection: str) -> int:
        col = self._get_collection(collection)
        return col.count()

    def delete(self, collection: str, ids: Sequence[str]) -> None:
        if not ids:
            return
        col = self._get_collection(collection)
        col.delete(ids=list(ids))

    def collection_info(self, collection: str) -> CollectionInfo:
        col = self._get_collection(collection)
        spec = self._specs.get(collection)
        return CollectionInfo(
            name=collection,
            dimension=spec.dimension if spec else None,
            count=col.count(),
            metric=spec.metric if spec else DistanceMetric.L2,
        )

    def health(self) -> VectorStoreHealth:
        try:
            self._client.heartbeat()
            return VectorStoreHealth(healthy=True)
        except Exception as exc:
            return VectorStoreHealth(healthy=False, detail=str(exc))

    def close(self) -> None:
        """ChromaDB PersistentClient has no explicit close."""
        pass
