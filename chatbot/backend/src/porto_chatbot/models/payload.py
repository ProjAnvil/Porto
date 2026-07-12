from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RagSettingsPayload(BaseModel):
    embedding_provider: Literal["local", "ollama"] | None = None
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    chunk_size: int | None = Field(default=None, ge=200, le=8000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=2000)
    top_k: int | None = Field(default=None, ge=1, le=30)


class AgentSettingsPayload(BaseModel):
    # LLM 连接
    agent_provider: Literal["openai", "anthropic"] | None = None
    agent_model: str | None = None
    agent_base_url: str | None = None
    agent_api_key: str | None = None
    agent_temperature: float | None = Field(default=None, ge=0, le=2)
    agent_max_tokens: int | None = Field(default=None, ge=1, le=128000)
    # Critic（独立评审模型，可选；未配则复用上面的 agent_*）
    critic_provider: Literal["openai", "anthropic"] | None = None
    critic_model: str | None = None
    critic_base_url: str | None = None
    critic_api_key: str | None = None
    critic_temperature: float | None = Field(default=None, ge=0, le=2)
    critic_max_tokens: int | None = Field(default=None, ge=1, le=128000)
    # Spec refine loop
    spec_refine_enabled: bool | None = None
    spec_refine_max_iter: int | None = Field(default=None, ge=0, le=10)
    spec_refine_parallel: bool | None = None
    spec_refine_pass_score: int | None = Field(default=None, ge=0, le=12)
    spec_refine_budget_tokens: int | None = Field(default=None, ge=1000)
    # Workflow 条件回边
    workflow_rework_enabled: bool | None = None
    workflow_rework_max_passes: int | None = Field(default=None, ge=0, le=5)
    # Memory compaction
    memory_compact_threshold: int | None = Field(default=None, ge=4)
    memory_recent_keep: int | None = Field(default=None, ge=1)
    # Context 预算 / 流式 / 节点 tool 轮数
    context_char_budget: int | None = Field(default=None, ge=1000)
    agent_stream_enabled: bool | None = None
    agent_max_tool_turns: int | None = Field(default=None, ge=1, le=20)


class AppSettingsPayload(BaseModel):
    rag: RagSettingsPayload | None = None
    agent: AgentSettingsPayload | None = None


class AppSettingsResponse(BaseModel):
    rag: RagSettingsPayload
    agent: AgentSettingsPayload


class IndexRequest(RagSettingsPayload):
    reset: bool = True


class WorkflowRequest(BaseModel):
    text: str | None = None
    project_name: str | None = None
    top_k: int | None = None
    session_id: str = "default"
    rag: RagSettingsPayload | None = None
    agent: AgentSettingsPayload | None = None


class EvalCase(BaseModel):
    question: str
    answer: str
    contexts: list[str] = Field(default_factory=list)
    ground_truth: str | None = None


class EvalRequest(BaseModel):
    cases: list[EvalCase]
