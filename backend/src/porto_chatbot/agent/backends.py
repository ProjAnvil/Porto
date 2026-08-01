# backend/src/porto_chatbot/agent/backends.py
"""AgentBackend Protocol + LangchainBackend + NodeExecutionResult.

Strategy pattern: chatbot 和 workflow 的所有 LLM 交互经过这个接口。
加新引擎 = 实现这个 Protocol，不改任何调用方。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..llm import LLMClient
from ..llm.types import ToolDef
from ..logging_utils import get_component_logger
from ..models import ChatRequest, ChatResponse
from ..settings import Settings
from ..tools import AgentToolContext, build_agent_tools


@dataclass
class NodeExecutionResult:
    """节点执行的统一返回格式——消费方不关心后端差异。"""

    text: str = ""
    structured: dict | None = None
    tool_calls: list = field(default_factory=list)
    turns: int = 0
    truncated: bool = False
    reason: str | None = None


# BackendTools: list[ToolDef] (langchain) or SDK MCP server (agent_sdk)
BackendTools = Any


@runtime_checkable
class AgentBackend(Protocol):
    """一个执行引擎的完整契约。"""

    def build_tools(self, ctx: AgentToolContext) -> BackendTools:
        """返回该引擎的工具集。"""
        ...

    async def execute_node(
        self,
        *,
        system: str,
        user: str,
        tools: BackendTools | None = None,
        structured_schema: dict | None = None,
        max_turns: int = 10,
    ) -> NodeExecutionResult:
        """一次节点级 agent 调用。

        三种模式由参数控制：
        - tools 非空 → tool-calling loop（understand, generate_initial_spec）
        - tools 空但 structured_schema 非空 → 结构化输出（identify, critique）
        - 都空 → 纯文本补全（refine_spec）
        """
        ...

    async def chat(self, req: ChatRequest, settings: Settings) -> ChatResponse:
        """chatbot 模式的完整处理。"""
        ...

    async def chat_stream(
        self, req: ChatRequest, settings: Settings
    ) -> AsyncIterator[str]:
        """SSE 流式版。"""
        ...


class LangchainBackend:
    """现有 complete_with_tools / complete_structured / complete 的薄封装。

    行为和直接调 LLMClient 完全一样——只是走接口。
    """

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.logger = get_component_logger("backend.langchain", llm.settings)

    def build_tools(self, ctx: AgentToolContext) -> list[ToolDef]:
        return build_agent_tools(ctx)

    async def execute_node(
        self,
        *,
        system: str,
        user: str,
        tools: BackendTools | None = None,
        structured_schema: dict | None = None,
        max_turns: int = 10,
    ) -> NodeExecutionResult:
        if tools:
            r = self.llm.complete_with_tools(system, user, tools, max_turns=max_turns)
            return NodeExecutionResult(
                text=r.text,
                tool_calls=list(r.tool_calls),
                turns=r.turns,
                truncated=r.truncated,
                reason=r.reason,
            )
        if structured_schema:
            parsed = self.llm.complete_structured(system, user, structured_schema)
            return NodeExecutionResult(
                structured=parsed,
                text=json.dumps(parsed, ensure_ascii=False) if parsed else "",
            )
        text = self.llm.complete(system, user)
        return NodeExecutionResult(text=text or "")

    async def chat(self, req: ChatRequest, settings: Settings) -> ChatResponse:
        """Langchain chatbot 逻辑——委托给现有 chat.py 的 _langchain_chat。"""
        from .langchain_chat import langchain_chat

        return langchain_chat(req, settings)

    async def chat_stream(
        self, req: ChatRequest, settings: Settings
    ) -> AsyncIterator[str]:
        from .langchain_chat import langchain_chat_stream

        async for chunk in langchain_chat_stream(req, settings):
            yield chunk
