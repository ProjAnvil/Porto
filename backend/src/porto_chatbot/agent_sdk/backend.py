# backend/src/porto_chatbot/agent_sdk/backend.py
"""AgentSDKBackend: Claude Agent SDK implementation of AgentBackend.

Uses ``ClaudeSDKClient`` + custom ``@tool`` functions + ``setting_sources``
for skill discovery. Implements the same ``AgentBackend`` Protocol as
``LangchainBackend`` so the factory can swap them transparently.

The ``claude_agent_sdk`` imports are top-level but defensive: when the SDK is
not installed, every relevant name becomes ``None``. This keeps the module
importable (so ``agent_sdk.tools.build_sdk_tools`` still works as a probe)
while ``execute_node`` will raise ``RuntimeError`` if actually invoked without
the SDK.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..agent.backends import BackendTools, NodeExecutionResult
from ..logging_utils import get_component_logger
from ..models import ChatRequest, ChatResponse
from ..settings import Settings
from ..tools.context import AgentToolContext
from .tools import build_sdk_tools

# Defensive SDK imports: the package must stay importable so that
# ``build_sdk_tools`` can feature-detect availability and so that tests can
# patch these names at module level even when running offline.
try:
    from claude_agent_sdk import (  # type: ignore[generic]
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
        create_sdk_mcp_server,
    )
except ImportError:  # SDK not installed — AgentSDKBackend is unusable
    AssistantMessage = None  # type: ignore[assignment]
    ClaudeAgentOptions = None  # type: ignore[assignment]
    ClaudeSDKClient = None  # type: ignore[assignment]
    ResultMessage = None  # type: ignore[assignment]
    TextBlock = None  # type: ignore[assignment]
    ToolUseBlock = None  # type: ignore[assignment]
    create_sdk_mcp_server = None  # type: ignore[assignment]


class AgentSDKBackend:
    """Claude Agent SDK engine.

    Uses ``ClaudeSDKClient`` + custom ``@tools`` + ``setting_sources`` for
    skill discovery. ``chat``/``chat_stream`` are stubbed and implemented in
    Task 7.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("backend.agent_sdk", settings)

    def build_tools(self, ctx: AgentToolContext) -> list:
        """Return the SDK ``@tool`` list bound to ``ctx``.

        Returns ``[]`` when ``claude_agent_sdk`` is not installed — callers
        can detect this and fall back to another backend.
        """
        return build_sdk_tools(ctx)

    async def execute_node(
        self,
        *,
        system: str,
        user: str,
        tools: BackendTools | None = None,
        structured_schema: dict | None = None,
        max_turns: int = 10,
    ) -> NodeExecutionResult:
        """Run one node-level agent turn through the Claude Agent SDK.

        Three modes (mirroring ``LangchainBackend.execute_node``):
        - ``tools`` non-empty → tool-calling loop via an in-process MCP server.
        - ``tools`` empty but ``structured_schema`` provided → structured output.
        - both empty → plain text completion.

        Exceptions are caught and returned as a truncated ``NodeExecutionResult``
        rather than propagated, so workflow orchestration can degrade gracefully.
        """
        if ClaudeSDKClient is None or ClaudeAgentOptions is None:
            raise RuntimeError(
                "claude_agent_sdk is not installed — AgentSDKBackend unavailable"
            )

        sdk_tools = tools if isinstance(tools, list) else []
        server = None
        if sdk_tools and create_sdk_mcp_server is not None:
            server = create_sdk_mcp_server(
                name="porto", version="1.0.0", tools=sdk_tools,
            )

        options_kwargs: dict[str, Any] = {
            "system_prompt": system,
            "max_turns": max_turns,
            "setting_sources": ["project"],
            "cwd": str(self.settings.data_dir),
        }
        if server is not None:
            options_kwargs["mcp_servers"] = {"porto": server}
            options_kwargs["allowed_tools"] = ["mcp__porto__*"]
        if structured_schema is not None:
            # SDK field is `output_format`; ResultMessage.structured_output
            # carries the parsed result.
            options_kwargs["output_format"] = structured_schema

        options = ClaudeAgentOptions(**options_kwargs)

        text = ""
        structured: dict | None = None
        tool_calls: list = []
        turns = 0
        truncated = False
        reason: str | None = None

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(user)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text += block.text
                            elif isinstance(block, ToolUseBlock):
                                tool_calls.append({
                                    "name": block.name,
                                    "arguments": block.input,
                                })
                    elif isinstance(msg, ResultMessage):
                        turns = getattr(msg, "num_turns", 0) or 0
                        if msg.subtype != "success":
                            truncated = True
                            reason = msg.subtype
                        # Prefer structured_output (parsed by the SDK when
                        # output_format is set); fall back to parsing result.
                        raw_structured = getattr(msg, "structured_output", None)
                        if raw_structured is not None and isinstance(
                            raw_structured, (dict, list)
                        ):
                            structured = raw_structured
                        elif structured_schema and getattr(msg, "result", None):
                            try:
                                parsed = json.loads(msg.result)
                                if isinstance(parsed, (dict, list)):
                                    structured = parsed
                            except (json.JSONDecodeError, TypeError):
                                pass
        except Exception as exc:
            self.logger.exception("agent_sdk execute_node failed")
            return NodeExecutionResult(
                text=f"Agent SDK 执行失败：{exc}",
                truncated=True,
                reason="agent_sdk_error",
            )

        return NodeExecutionResult(
            text=text,
            structured=structured,
            tool_calls=tool_calls,
            turns=turns,
            truncated=truncated,
            reason=reason,
        )

    async def chat(self, req: ChatRequest, settings: Settings) -> ChatResponse:
        """Chatbot-mode entry — implemented in Task 7."""
        raise NotImplementedError("AgentSDKBackend.chat is implemented in Task 7")

    async def chat_stream(
        self, req: ChatRequest, settings: Settings
    ) -> AsyncIterator[str]:
        """SSE streaming chatbot entry — implemented in Task 7."""
        raise NotImplementedError(
            "AgentSDKBackend.chat_stream is implemented in Task 7"
        )
        yield  # pragma: no cover — make this a generator for type checkers
