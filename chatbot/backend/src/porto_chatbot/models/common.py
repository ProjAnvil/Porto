from __future__ import annotations

from typing import Any, Literal

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


IndexJobState = Literal["idle", "running", "succeeded", "failed", "interrupted"]


class IndexJobStatus(BaseModel):
    """RAG 索引任务的持久化状态视图（对应 service_locks['rag_index'] 行）。"""

    status: IndexJobState = "idle"
    source: str | None = None
    reset: bool = True
    progress_done: int = 0
    progress_total: int = 0
    chunks_done: int = 0
    started_at: str | None = None
    heartbeat_at: str | None = None
    finished_at: str | None = None
    last_indexed_at: str | None = None
    last_stats: IndexStats | None = None
    error: str | None = None


DependencyName = Literal["embedding", "agent_llm", "critic_llm"]
DependencyStatus = Literal["ok", "degraded", "down", "unknown"]
FeatureName = Literal["chat", "rag_search", "workflow"]


class DependencyHealth(BaseModel):
    name: DependencyName
    status: DependencyStatus = "unknown"
    latency_ms: float | None = None
    detail: str | None = None
    checked_at: str | None = None


class FeatureAvailability(BaseModel):
    name: FeatureName
    available: bool = True
    reason: str | None = None


class HealthSnapshot(BaseModel):
    dependencies: list[DependencyHealth] = Field(default_factory=list)
    features: list[FeatureAvailability] = Field(default_factory=list)
    rag_index: IndexJobStatus = Field(default_factory=IndexJobStatus)
    updated_at: str | None = None


class IndexStatsView(IndexStats):
    """索引统计 + RAG 任务状态，供前端 `/api/kb/stats` 轮询。"""

    rag_index: IndexJobStatus = Field(default_factory=IndexJobStatus)
