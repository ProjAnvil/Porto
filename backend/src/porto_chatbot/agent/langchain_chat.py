# backend/src/porto_chatbot/agent/langchain_chat.py
"""Langchain chatbot entry points — thin wrappers around ChatOrchestrator.

Task 6: 核心逻辑搬到 ``agent/orchestrator.py``（ChatOrchestrator）+
``agent/langchain_chat_stream.py``（SSE 流式生成）。本文件只保留：
- ``langchain_chat(req, settings)`` / ``langchain_chat_stream(req, settings)``
  两个入口，构造 ChatOrchestrator 并委派。
- ``_trim_to_budget`` 从 orchestrator 模块再导出（旧 import 不破坏）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ..api.deps import (
    get_conversation_memory,
    get_session_store,
    get_store,
)
from ..models import ChatRequest, ChatResponse
from .orchestrator import ChatOrchestrator, _trim_to_budget

__all__ = [
    "langchain_chat",
    "langchain_chat_stream",
    "_trim_to_budget",
]


def langchain_chat(req: ChatRequest, settings) -> ChatResponse:
    """Langchain chatbot 同步入口。"""
    sessions = get_session_store(settings)
    memory = get_conversation_memory(settings)
    kb_store = get_store(settings)
    orch = ChatOrchestrator(sessions, memory, kb_store, settings)
    return orch.handle(req)


async def langchain_chat_stream(req: ChatRequest, settings) -> AsyncIterator[str]:
    """Langchain chatbot 流式入口。"""
    sessions = get_session_store(settings)
    memory = get_conversation_memory(settings)
    kb_store = get_store(settings)
    orch = ChatOrchestrator(sessions, memory, kb_store, settings)
    async for chunk in orch.handle_stream(req):
        yield chunk
