"""Public exports for the vector-store abstraction layer."""
from .base import (
    CollectionInfo,
    CollectionNotReadyError,
    CollectionSpec,
    DimensionMismatchError,
    DistanceMetric,
    VectorRecord,
    VectorRepository,
    VectorSearchHit,
    VectorSearchRequest,
    VectorStoreConnectionError,
    VectorStoreError,
    VectorStoreHealth,
)
from .chroma import ChromaVectorRepository

__all__ = [
    "CollectionInfo",
    "CollectionNotReadyError",
    "CollectionSpec",
    "DimensionMismatchError",
    "DistanceMetric",
    "VectorRecord",
    "VectorRepository",
    "VectorSearchHit",
    "VectorSearchRequest",
    "VectorStoreConnectionError",
    "VectorStoreError",
    "VectorStoreHealth",
    "ChromaVectorRepository",
]
