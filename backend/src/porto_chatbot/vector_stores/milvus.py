"""PyMilvus adapter implementing the :class:`VectorRepository` protocol.

Uses explicit schemas, HNSW index (M=16, efConstruction=200, search ef=128),
collection load, flush, and Strong-consistency reads.
"""
from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from typing import Any

from pymilvus import (
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
)

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

logger = get_component_logger("vector_stores.milvus")

# HNSW index parameters
_HNSW_M = 16
_HNSW_EF_CONSTRUCTION = 200
_SEARCH_EF = 128

# Milvus scalar field size limit
_MAX_VARCHAR_LENGTH = 65535

_METRIC_MAP: dict[DistanceMetric, str] = {
    DistanceMetric.L2: "L2",
    DistanceMetric.COSINE: "COSINE",
}


def _normalize_score(distance: float, metric: DistanceMetric) -> float:
    """Convert Milvus distance to higher-is-better score."""
    if metric == DistanceMetric.L2:
        return math.exp(-distance)
    # Milvus COSINE returns similarity directly (higher = better)
    return max(0.0, distance)


class MilvusVectorRepository:
    """PyMilvus-backed implementation of :class:`VectorRepository`."""

    def __init__(
        self,
        uri: str,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
        tls: bool = False,
        timeout: int = 30,
    ):
        self._timeout = timeout
        self._specs: dict[str, CollectionSpec] = {}

        connect_kwargs: dict[str, Any] = {"uri": uri, "timeout": timeout}
        if token:
            connect_kwargs["token"] = token
        if username and password:
            connect_kwargs["username"] = username
            connect_kwargs["password"] = password
        if database:
            connect_kwargs["db_name"] = database
        if tls:
            connect_kwargs["secure"] = True

        try:
            self._client = MilvusClient(**connect_kwargs)
        except Exception as exc:
            raise VectorStoreConnectionError(
                f"Failed to connect to Milvus at {uri}",
            ) from exc
        logger.info("milvus repository connected uri=%s", uri)

    @classmethod
    def from_profile(cls, profile: Any) -> MilvusVectorRepository:
        """Construct from a :class:`VectorProfile`-like object."""
        return cls(
            uri=profile.uri,
            token=getattr(profile, "_token", None),
            username=getattr(profile, "_username", None),
            password=getattr(profile, "_password", None),
            database=profile.database,
            tls=profile.tls,
            timeout=profile.timeout,
        )

    # ── internal helpers ──

    def _discover_dimension(self, name: str) -> int | None:
        """Get the vector field dimension of an existing Milvus collection."""
        try:
            info = self._client.describe_collection(name)
            for f in info.get("fields", []):
                dim = f.get("params", {}).get("dim")
                if dim is not None:
                    return int(dim)
        except Exception:
            pass
        return None

    def _check_dimension(self, collection: str, records: Sequence[VectorRecord]) -> None:
        spec = self._specs.get(collection)
        if spec is None:
            return
        for record in records:
            if len(record.embedding) != spec.dimension:
                raise DimensionMismatchError(
                    f"Collection {collection!r} expects dimension {spec.dimension}, "
                    f"record {record.id!r} has {len(record.embedding)}",
                )

    @staticmethod
    def _validate_scalar(value: Any, field: str) -> None:
        """Reject oversized varchar/json fields before sending."""
        if isinstance(value, str) and len(value.encode("utf-8")) > _MAX_VARCHAR_LENGTH:
            raise ValueError(
                f"Field {field!r} exceeds {_MAX_VARCHAR_LENGTH}-byte Milvus scalar limit",
            )

    def _build_filter(self, where: dict[str, Any] | None) -> str | None:
        """Convert a simple ``where`` dict to a Milvus filter expression."""
        if not where:
            return None
        parts: list[str] = []
        for key, val in where.items():
            if isinstance(val, str):
                # Escape single quotes
                escaped = val.replace("'", "\\'")
                parts.append(f"{key} == '{escaped}'")
            elif isinstance(val, bool):
                parts.append(f"{key} == {str(val).lower()}")
            elif isinstance(val, (int, float)):
                parts.append(f"{key} == {val}")
            else:
                parts.append(f"{key} == {val!r}")
        return " and ".join(parts) if parts else None

    # ── protocol implementation ──

    def ensure_collection(self, spec: CollectionSpec) -> None:
        self._specs[spec.name] = spec

        # Check if collection already exists
        if self._client.has_collection(spec.name):
            self._client.load_collection(spec.name)
            return

        pk = FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=256)
        vector = FieldSchema(
            name="vector",
            dtype=DataType.FLOAT_VECTOR,
            dim=spec.dimension,
        )
        document = FieldSchema(name="document", dtype=DataType.VARCHAR, max_length=_MAX_VARCHAR_LENGTH)
        # Store metadata as JSON
        metadata = FieldSchema(name="metadata", dtype=DataType.JSON)

        schema = CollectionSchema(
            fields=[pk, vector, document, metadata],
            description=f"Porto {spec.metadata.get('kind', 'unknown')} collection",
        )

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type=_METRIC_MAP.get(spec.metric, "L2"),
            params={"M": _HNSW_M, "efConstruction": _HNSW_EF_CONSTRUCTION},
        )

        self._client.create_collection(
            collection_name=spec.name,
            schema=schema,
            index_params=index_params,
        )
        self._client.load_collection(spec.name)
        logger.info(
            "collection ensured name=%s metric=%s dim=%s",
            spec.name, spec.metric, spec.dimension,
        )

    def upsert(self, collection: str, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        self._check_dimension(collection, records)
        import json

        data: list[dict[str, Any]] = []
        for r in records:
            self._validate_scalar(r.document, "document")
            data.append({
                "id": r.id,
                "vector": r.embedding,
                "document": r.document,
                "metadata": json.dumps(r.metadata),
            })
        try:
            self._client.upsert(collection_name=collection, data=data)
        except Exception as exc:
            if "dimension" in str(exc).lower():
                raise DimensionMismatchError(str(exc)) from exc
            raise

    def search(
        self, collection: str, request: VectorSearchRequest,
    ) -> list[VectorSearchHit]:
        spec = self._specs.get(collection)
        metric = spec.metric if spec else DistanceMetric.L2

        search_params: dict[str, Any] = {
            "metric_type": _METRIC_MAP.get(metric, "L2"),
            "params": {"ef": _SEARCH_EF},
        }

        filter_expr = self._build_filter(request.where)
        search_kwargs: dict[str, Any] = {
            "collection_name": collection,
            "data": [request.query_embedding],
            "limit": request.top_k,
            "search_params": search_params,
            "output_fields": ["id", "document", "metadata"],
        }
        if filter_expr:
            search_kwargs["filter"] = filter_expr

        try:
            results = self._client.search(**search_kwargs)
        except Exception as exc:
            if "dimension" in str(exc).lower():
                raise DimensionMismatchError(str(exc)) from exc
            if "not found" in str(exc).lower() or "not loaded" in str(exc).lower():
                raise CollectionNotReadyError(str(exc)) from exc
            raise

        if not results:
            return []

        import json

        hits: list[VectorSearchHit] = []
        for hit in results[0]:
            entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
            hit_id = hit.get("id", "") if isinstance(hit, dict) else ""
            distance = hit.get("distance", 0.0) if isinstance(hit, dict) else 0.0
            doc = entity.get("document", "") or ""
            meta_raw = entity.get("metadata", "{}")
            try:
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) else dict(meta_raw or {})
            except (json.JSONDecodeError, TypeError):
                meta = {}
            hits.append(
                VectorSearchHit(
                    id=str(hit_id),
                    document=doc,
                    metadata=meta,
                    score=_normalize_score(float(distance), metric),
                )
            )
        return hits

    def fetch(self, collection: str, ids: Sequence[str]) -> list[VectorRecord]:
        if not ids:
            return []
        import json

        quoted_ids = ", ".join(f"'{i}'" for i in ids)
        try:
            results = self._client.query(
                collection_name=collection,
                filter=f"id in [{quoted_ids}]",
                output_fields=["id", "vector", "document", "metadata"],
            )
        except Exception as exc:
            if "not found" in str(exc).lower():
                raise CollectionNotReadyError(str(exc)) from exc
            raise

        records: list[VectorRecord] = []
        for row in results:
            row_dict = row if isinstance(row, dict) else dict(row)
            meta_raw = row_dict.get("metadata", "{}")
            try:
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) else dict(meta_raw or {})
            except (json.JSONDecodeError, TypeError):
                meta = {}
            records.append(
                VectorRecord(
                    id=str(row_dict.get("id", "")),
                    embedding=list(row_dict.get("vector", [])),
                    document=row_dict.get("document", "") or "",
                    metadata=meta,
                )
            )
        return records

    def export_batches(
        self, collection: str, batch_size: int,
    ) -> Iterator[list[VectorRecord]]:
        """Yield primary-key-ascending batches bounded by an ID snapshot."""
        # Capture snapshot of all IDs
        try:
            all_rows = self._client.query(
                collection_name=collection,
                filter="",
                output_fields=["id"],
                limit=16384,
            )
        except Exception as exc:
            if "not found" in str(exc).lower():
                raise CollectionNotReadyError(str(exc)) from exc
            raise

        snapshot = sorted(
            str(row.get("id", "")) if isinstance(row, dict) else str(row)
            for row in (all_rows or [])
        )
        for i in range(0, len(snapshot), batch_size):
            yield self.fetch(collection, snapshot[i : i + batch_size])

    def count(self, collection: str) -> int:
        try:
            stats = self._client.get_collection_stats(collection)
            return int(stats.get("row_count", 0))
        except Exception as exc:
            if "not found" in str(exc).lower():
                raise CollectionNotReadyError(str(exc)) from exc
            raise

    def delete(self, collection: str, ids: Sequence[str]) -> None:
        if not ids:
            return
        quoted_ids = ", ".join(f"'{i}'" for i in ids)
        self._client.delete(
            collection_name=collection,
            filter=f"id in [{quoted_ids}]",
        )

    def collection_info(self, collection: str) -> CollectionInfo:
        spec = self._specs.get(collection)
        dimension = spec.dimension if spec else None
        # When _specs is empty (fresh repo during migration), discover the
        # actual dimension from the collection schema.
        if dimension is None:
            dimension = self._discover_dimension(collection)
        try:
            stats = self._client.get_collection_stats(collection)
            count = int(stats.get("row_count", 0))
        except Exception:
            count = 0
        return CollectionInfo(
            name=collection,
            dimension=dimension,
            count=count,
            metric=spec.metric if spec else DistanceMetric.L2,
        )

    def health(self) -> VectorStoreHealth:
        try:
            ok = self._client.get_server_version()
            return VectorStoreHealth(healthy=True, detail=f"Milvus {ok}")
        except Exception as exc:
            return VectorStoreHealth(healthy=False, detail=str(exc))

    def list_collections(self) -> list[str]:
        try:
            return self._client.list_collections()
        except Exception:
            return []

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    # ── Milvus-specific (not part of the protocol) ──

    def drop_collection(self, name: str) -> None:
        try:
            self._client.drop_collection(name)
        except Exception:
            logger.info("collection drop skipped name=%s", name)
        self._specs.pop(name, None)

    def flush(self, collection: str) -> None:
        """Flush data to disk and refresh segments."""
        try:
            self._client.flush(collection)
        except Exception:
            logger.warning("flush failed collection=%s", collection)
