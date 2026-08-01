# backend/src/porto_chatbot/agent/langchain_chat.py
"""Langchain chatbot 逻辑的委托入口。

Phase 2 (Task 9) 会把 chat.py 的现有函数体搬到这里。
目前是占位——实际调用 chat.py 的现有函数。
"""
from __future__ import annotations

from ..models import ChatRequest, ChatResponse
from ..settings import Settings


def langchain_chat(req: ChatRequest, settings: Settings) -> ChatResponse:
    """Phase 2 (Task 9) 实现：从 chat.py 搬入现有 chat() 函数体。"""
    raise NotImplementedError("Implemented in Task 9")


async def langchain_chat_stream(req: ChatRequest, settings: Settings):
    """Phase 2 (Task 9) 实现。"""
    raise NotImplementedError("Implemented in Task 9")
    yield  # make it a generator  # pragma: no cover
