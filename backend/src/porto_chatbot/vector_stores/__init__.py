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
from .factory import VectorRepositoryFactory
from .milvus import MilvusVectorRepository

__all__ = [
    "CollectionInfo",
    "CollectionNotReadyError",
    "CollectionSpec",
    "DimensionMismatchError",
    "DistanceMetric",
    "VectorRecord",
    "VectorRepository",
    "VectorRepositoryFactory",
    "VectorSearchHit",
    "VectorSearchRequest",
    "VectorStoreConnectionError",
    "VectorStoreError",
    "VectorStoreHealth",
    "ChromaVectorRepository",
    "MilvusVectorRepository",
]
