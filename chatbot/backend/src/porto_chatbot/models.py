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


class RagSettingsPayload(BaseModel):
    embedding_provider: Literal["local", "ollama"] | None = None
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    chunk_size: int | None = Field(default=None, ge=200, le=8000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=2000)
    top_k: int | None = Field(default=None, ge=1, le=30)


class AgentSettingsPayload(BaseModel):
    agent_provider: Literal["openai", "anthropic"] | None = None
    agent_model: str | None = None
    agent_base_url: str | None = None
    agent_api_key: str | None = None
    agent_temperature: float | None = Field(default=None, ge=0, le=2)
    agent_max_tokens: int | None = Field(default=None, ge=1, le=128000)


class AppSettingsPayload(BaseModel):
    rag: RagSettingsPayload | None = None
    agent: AgentSettingsPayload | None = None


class AppSettingsResponse(BaseModel):
    rag: RagSettingsPayload
    agent: AgentSettingsPayload


class IndexRequest(RagSettingsPayload):
    reset: bool = True


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = Field(default_factory=list)
    top_k: int | None = None
    session_id: str = "default"
    rag: RagSettingsPayload | None = None
    agent: AgentSettingsPayload | None = None


class AgentStep(BaseModel):
    name: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceChunk]
    steps: list[AgentStep]
    evaluation: dict[str, Any] = Field(default_factory=dict)
    memory: list[SourceChunk] = Field(default_factory=list)


class WorkflowRequest(BaseModel):
    text: str | None = None
    project_name: str | None = None
    top_k: int | None = None
    session_id: str = "default"
    rag: RagSettingsPayload | None = None
    agent: AgentSettingsPayload | None = None


class MemoryRecord(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemorySearchResponse(BaseModel):
    query: str
    results: list[SourceChunk]


class EvalCase(BaseModel):
    question: str
    answer: str
    contexts: list[str] = Field(default_factory=list)
    ground_truth: str | None = None


class EvalRequest(BaseModel):
    cases: list[EvalCase]


class Subsystem(BaseModel):
    name: str
    type: Literal["new", "extend", "existing"] = "new"
    responsibility: str
    capabilities: list[str] = Field(default_factory=list)
    data_entities: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


class WorkflowResponse(BaseModel):
    workflow_id: str
    project_name: str
    understanding: str
    subsystems: list[Subsystem]
    specs: dict[str, str]
    evaluation: dict[str, Any]
    sources: list[SourceChunk]
    steps: list[AgentStep]


# ----------------------------- Spec refine loop（Phase 2）----------------------------- #

Verdict = Literal["PASS", "NEEDS_IMPROVEMENT", "FAIL"]


class Critique(BaseModel):
    """critic 对单个 spec 版本的评判结果。"""

    verdict: Verdict
    score: int = Field(ge=0, le=12)
    feedback: str = ""
    per_dimension: dict[str, int] = Field(default_factory=dict)


class SpecAttempt(BaseModel):
    """loop 中一次迭代的快照（供可观测，不含完整 spec 全文）。"""

    version: int
    score: int = 0
    verdict: Verdict = "NEEDS_IMPROVEMENT"
    feedback_digest: str = ""


class SpecResult(BaseModel):
    """单个子系统 spec 的 loop 产物。"""

    final: str
    attempts: list[SpecAttempt] = Field(default_factory=list)
    iterations: int = 0
    truncated: bool = False
    used_llm: bool = False
