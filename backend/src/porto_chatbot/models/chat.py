from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .common import SourceChunk
from .enums import ChatRole, FactCategory, FactStatus
from .payload import AgentSettingsPayload, RagSettingsPayload
from .workflow import AgentStep


class ChatMessage(BaseModel):
    role: ChatRole
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = Field(default_factory=list)
    top_k: int | None = None
    session_id: str = "default"
    rag: RagSettingsPayload | None = None
    agent: AgentSettingsPayload | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceChunk]
    steps: list[AgentStep]
    evaluation: dict[str, Any] = Field(default_factory=dict)
    memory: list[SourceChunk] = Field(default_factory=list)


class MemoryRecord(BaseModel):
    id: str
    session_id: str
    role: ChatRole
    content: str
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemorySearchResponse(BaseModel):
    query: str
    results: list[SourceChunk]


class SessionFact(BaseModel):
    """会话级关键事实(a2)。每条独立原子化,带 category 优先级与 status 生命周期。"""

    id: str
    session_id: str
    category: FactCategory
    content: str
    status: FactStatus = FactStatus.ACTIVE
    source_msg_id: str | None = None
    created_at: str
    updated_at: str
