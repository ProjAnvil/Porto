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
