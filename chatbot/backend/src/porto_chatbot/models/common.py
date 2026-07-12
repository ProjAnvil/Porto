from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SourceChunk(BaseModel):
    id: str
    path: str
    title: str
    text: str
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class IndexStats(BaseModel):
    kb_path: str
    documents: int
    chunks: int
    backend: str = "chroma"
    embedding_provider: str = "local"
    embedding_model: str = ""
    embedding_dimensions: int | None = None
    chunk_size: int = 0
    chunk_overlap: int = 0
