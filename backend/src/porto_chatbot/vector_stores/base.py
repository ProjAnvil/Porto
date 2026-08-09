"""Backend-neutral vector repository contract.

Canonical dataclasses, typed errors, and a runtime-checkable protocol that
every vector backend (Chroma, Milvus, …) must implement.  Services above this
layer consume only these types and never touch a concrete client.
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class DistanceMetric(StrEnum):
    """Distance / space configuration for a collection."""

    L2 = "l2"
    COSINE = "cosine"


@dataclass(frozen=True)
class VectorRecord:
    """One canonical vector row — the single write payload."""

    id: str
    embedding: list[float]
    document: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CollectionSpec:
    """Blueprint for creating / validating a collection."""

    name: str
    dimension: int
    metric: DistanceMetric = DistanceMetric.L2
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CollectionInfo:
    """Read-only snapshot of a collection's current state."""

    name: str
    dimension: int | None
    count: int
    metric: DistanceMetric


@dataclass(frozen=True)
class VectorSearchRequest:
    """Parameters for a similarity search."""

    query_embedding: list[float]
    top_k: int
    where: dict[str, Any] | None = None


@dataclass(frozen=True)
class VectorSearchHit:
    """Search result row — no embedding, but carries a score (higher = better)."""

    id: str
    document: str
    metadata: dict[str, Any]
    score: float


@dataclass(frozen=True)
class VectorStoreHealth:
    """Health-check result."""

    healthy: bool
    detail: str = ""


# ── typed errors ──


class VectorStoreError(Exception):
    """Base class for all repository errors."""


class DimensionMismatchError(VectorStoreError):
    """Raised when an upsert / search embedding has the wrong dimension."""


class CollectionNotReadyError(VectorStoreError):
    """Raised when a collection is missing or not loaded."""


class VectorStoreConnectionError(VectorStoreError):
    """Raised when the underlying store is unreachable."""


@runtime_checkable
class VectorRepository(Protocol):
    """Every vector backend implements this protocol."""

    def ensure_collection(self, spec: CollectionSpec) -> None: ...

    def upsert(self, collection: str, records: Sequence[VectorRecord]) -> None: ...

    def search(self, collection: str, request: VectorSearchRequest) -> list[VectorSearchHit]: ...

    def fetch(self, collection: str, ids: Sequence[str]) -> list[VectorRecord]: ...

    def export_batches(
        self, collection: str, batch_size: int,
    ) -> Iterator[list[VectorRecord]]: ...

    def count(self, collection: str) -> int: ...

    def delete(self, collection: str, ids: Sequence[str]) -> None: ...

    def collection_info(self, collection: str) -> CollectionInfo: ...

    def health(self) -> VectorStoreHealth: ...

    def close(self) -> None: ...
