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
        HookMatcher,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
        create_sdk_mcp_server,
    )
except ImportError:  # SDK not installed — AgentSDKBackend is unusable
    AssistantMessage = None  # type: ignore[assignment]
    ClaudeAgentOptions = None  # type: ignore[assignment]
    ClaudeSDKClient = None  # type: ignore[assignment]
    HookMatcher = None  # type: ignore[assignment]
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

    # ------------------------------------------------------------------ #
    # Chatbot-mode entry points (Task 7)
    # ------------------------------------------------------------------ #
    def _build_chat_options(
        self, req: ChatRequest, settings: Settings
    ) -> tuple[Any, dict[str, str]]:
        """Build ``ClaudeAgentOptions`` + mutable state for chat/chat_stream.

        Returns ``(options, state)`` where ``state['answer_text']`` is updated
        by the caller as ``AssistantMessage`` TextBlocks arrive. The Stop hook
        closure reads ``state['answer_text']`` to persist the full conversation
        turn to :class:`MemoryStore` and trigger facts extraction.

        No intent routing — Claude decides autonomously via the registered
        MCP tools (search_knowledgebase, search_memory, get_session_facts…).
        """
        from ..api.deps import get_memory, get_store
        from ..llm import LLMClient
        from ..memory import SessionFactsStore, trigger_facts_extraction_sync

        store = get_store(settings)
        memory = get_memory(settings)
        facts_store = SessionFactsStore(settings)
        store.ensure_index()

        ctx = AgentToolContext(
            state={},
            vector_store=store,
            memory_store=memory,
            facts_store=facts_store,
        )
        sdk_tools = build_sdk_tools(ctx)
        server = create_sdk_mcp_server(
            name="porto", version="1.0.0", tools=sdk_tools,
        )

        # Mutable container shared between caller (accumulates text) and the
        # Stop hook (reads the final text). A plain dict avoids ``nonlocal``
        # plumbing and works naturally with the helper extraction.
        state: dict[str, str] = {"answer_text": ""}

        async def on_stop(input_data, tool_use_id, context):  # noqa: ANN001
            """Stop hook: persist user+assistant turn, then trigger facts extraction.

            Fail-open — any exception is logged but never propagated, so a
            memory/facts hiccup cannot crash the chat response.
            """
            try:
                memory.add(
                    session_id=req.session_id, role="user", content=req.message,
                )
                if state["answer_text"]:
                    memory.add(
                        session_id=req.session_id,
                        role="assistant",
                        content=state["answer_text"],
                    )
                trigger_facts_extraction_sync(
                    store=facts_store,
                    llm=LLMClient(settings),
                    session_id=req.session_id,
                    new_message=req.message,
                    recent_turns=[],
                    settings=settings,
                )
            except Exception:
                self.logger.exception(
                    "stop hook failed session=%s", req.session_id,
                )
            return {}  # no-op hook JSON output (SyncHookJSONOutput is all-optional)

        options = ClaudeAgentOptions(
            system_prompt=(
                "你是 Porto 知识库问答助手。你可以调用工具检索知识库、对话记忆和结构化事实。"
                "优先基于工具返回的信息回答；不确定时说明缺口。"
                f"当前 session_id: {req.session_id}"
            ),
            setting_sources=["project"],
            cwd=str(settings.data_dir),
            mcp_servers={"porto": server},
            allowed_tools=["mcp__porto__*"],
            max_turns=settings.agent_max_tool_turns,
            hooks={"Stop": [HookMatcher(matcher="", hooks=[on_stop])]},
        )
        return options, state

    async def chat(self, req: ChatRequest, settings: Settings) -> ChatResponse:
        """Chatbot-mode entry — Claude Agent SDK ReAct loop.

        No intent routing: Claude autonomously decides which tools to call
        (search_knowledgebase, search_memory, get_session_facts…). The Stop
        hook persists the conversation turn and triggers facts extraction.
        SDK failures degrade to a :class:`ChatResponse` with an error message
        instead of raising (callers get a user-friendly answer, never a 500).
        """
        if ClaudeSDKClient is None or ClaudeAgentOptions is None:
            return ChatResponse(
                answer="claude_agent_sdk 未安装，请在 Settings 切换到 Langchain 引擎。",
                sources=[],
                memory=[],
                evaluation={"score": 0.0, "passed": False, "cases": []},
                steps=[
                    {
                        "name": "agent_init",
                        "status": "failed",
                        "summary": "claude_agent_sdk not installed",
                        "data": {},
                    },
                ],
            )

        from ..api.deps import get_index_supervisor

        # RAG availability check — mirrors langchain_chat gating.
        available, reason = get_index_supervisor().rag_available()
        if not available:
            return ChatResponse(
                answer=f"知识库当前不可用（{reason}），请稍后重试。",
                sources=[],
                memory=[],
                evaluation={"score": 0.0, "passed": False, "cases": []},
                steps=[
                    {
                        "name": "rag_check",
                        "status": "completed",
                        "summary": f"rag unavailable: {reason}",
                        "data": {"reason": reason},
                    },
                ],
            )

        try:
            options, state = self._build_chat_options(req, settings)
            async with ClaudeSDKClient(options=options) as client:
                await client.query(req.message)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                state["answer_text"] += block.text
        except Exception as exc:
            self.logger.exception(
                "agent_sdk chat failed session=%s", req.session_id,
            )
            return ChatResponse(
                answer=f"Agent 引擎暂时不可用：{exc}。请在 Settings 切换到 Langchain 引擎。",
                sources=[],
                memory=[],
                evaluation={"score": 0.0, "passed": False, "cases": []},
                steps=[
                    {
                        "name": "agent_react",
                        "status": "failed",
                        "summary": str(exc),
                        "data": {},
                    },
                ],
            )

        answer_text = state["answer_text"] or "（Agent 未返回内容，请重试或检查配置）"
        return ChatResponse(
            answer=answer_text,
            sources=[],
            memory=[],
            evaluation={"score": 0.0, "passed": True, "cases": []},
            steps=[
                {
                    "name": "agent_react",
                    "status": "completed",
                    "summary": "Agent SDK ReAct loop",
                    "data": {},
                },
                {
                    "name": "answer",
                    "status": "completed",
                    "summary": "完成回答生成",
                    "data": {},
                },
            ],
        )

    async def chat_stream(
        self, req: ChatRequest, settings: Settings
    ) -> AsyncIterator[str]:
        """SSE streaming chatbot entry — wraps the SDK message stream.

        Emits ai-sdk SSE events (start → start-step → text-start →
        text-delta(s) → text-end → finish-step → finish → [DONE]) mirroring
        :func:`langchain_chat_stream`. RAG-unavailable and SDK-error paths
        emit a proper SSE error/finish sequence so the frontend stream always
        terminates cleanly.
        """
        from ..api.deps import get_index_supervisor
        from ..api.sse import _ai_sdk_sse, _text_chunks

        text_id = "answer-1"

        # -- RAG availability check (mirror chat's early return) -------------
        available, reason = get_index_supervisor().rag_available()
        if not available:
            hint = f"知识库当前不可用（{reason}），请稍后重试。"
            yield _ai_sdk_sse(
                {"type": "start", "messageMetadata": {"session_id": req.session_id}},
            )
            yield _ai_sdk_sse({"type": "start-step"})
            yield _ai_sdk_sse({"type": "text-start", "id": text_id})
            for chunk in _text_chunks(hint):
                yield _ai_sdk_sse(
                    {"type": "text-delta", "id": text_id, "delta": chunk},
                )
            yield _ai_sdk_sse({"type": "text-end", "id": text_id})
            yield _ai_sdk_sse({"type": "finish-step"})
            yield _ai_sdk_sse(
                {
                    "type": "finish",
                    "finishReason": "stop",
                    "messageMetadata": {"source_count": 0},
                },
            )
            yield "data: [DONE]\n\n"
            return

        if ClaudeSDKClient is None or ClaudeAgentOptions is None:
            yield _ai_sdk_sse(
                {"type": "start", "messageMetadata": {"session_id": req.session_id}},
            )
            yield _ai_sdk_sse({"type": "error", "errorText": "claude_agent_sdk 未安装"})
            yield _ai_sdk_sse({"type": "finish", "finishReason": "error"})
            yield "data: [DONE]\n\n"
            return

        yield _ai_sdk_sse(
            {"type": "start", "messageMetadata": {"session_id": req.session_id}},
        )
        yield _ai_sdk_sse({"type": "start-step"})
        yield _ai_sdk_sse({"type": "text-start", "id": text_id})

        try:
            options, state = self._build_chat_options(req, settings)
            async with ClaudeSDKClient(options=options) as client:
                await client.query(req.message)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text:
                                state["answer_text"] += block.text
                                yield _ai_sdk_sse(
                                    {
                                        "type": "text-delta",
                                        "id": text_id,
                                        "delta": block.text,
                                    },
                                )
        except Exception as exc:
            self.logger.exception(
                "agent_sdk chat_stream failed session=%s", req.session_id,
            )
            yield _ai_sdk_sse({"type": "error", "errorText": str(exc)})
            yield _ai_sdk_sse({"type": "text-end", "id": text_id})
            yield _ai_sdk_sse({"type": "finish-step"})
            yield _ai_sdk_sse({"type": "finish", "finishReason": "error"})
            yield "data: [DONE]\n\n"
            return

        yield _ai_sdk_sse({"type": "text-end", "id": text_id})
        yield _ai_sdk_sse({"type": "finish-step"})
        yield _ai_sdk_sse(
            {
                "type": "finish",
                "finishReason": "stop",
                "messageMetadata": {"source_count": 0},
            },
        )
        yield "data: [DONE]\n\n"
