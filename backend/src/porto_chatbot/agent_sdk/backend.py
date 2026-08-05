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

import asyncio
import json
import os
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ..agent.backends import BackendTools, NodeExecutionResult
from ..logging_utils import get_component_logger
from ..models import ChatRequest, ChatResponse
from ..models.enums import StepStatus
from ..settings import Settings
from ..tools.context import AgentToolContext
from .tools import build_sdk_tools


class ClaudeMsgSubtype(StrEnum):
    """Claude Agent SDK ``ResultMessage.subtype`` / ``SystemMessage.subtype`` 值。"""

    INIT = "init"
    SUCCESS = "success"


class AnthropicEventType(StrEnum):
    """Anthropic streaming event ``type`` 值（token-level streaming）。"""

    CONTENT_BLOCK_DELTA = "content_block_delta"


class AnthropicDeltaType(StrEnum):
    """Anthropic streaming ``delta.type`` 值。"""

    TEXT_DELTA = "text_delta"

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
        StreamEvent,
        SystemMessage,
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
    StreamEvent = None  # type: ignore[assignment]
    SystemMessage = None  # type: ignore[assignment]
    TextBlock = None  # type: ignore[assignment]
    ToolUseBlock = None  # type: ignore[assignment]
    create_sdk_mcp_server = None  # type: ignore[assignment]

# Process-level cache: Porto session_id → Claude Code CLI session_id.
# Enables conversation continuity across separate /api/chat/stream requests.
_claude_session_map: dict[str, str] = {}


async def _receive_with_idle_timeout(
    response_gen: AsyncIterator, timeout: float,
) -> AsyncIterator:
    """Wrap an SDK response async iterator with per-message idle timeout.

    If no message arrives within ``timeout`` seconds, raises
    :class:`asyncio.TimeoutError` — propagated to the caller's try/except
    for error-response handling. Prevents indefinite hangs when the Claude
    CLI subprocess stops producing stdout output (known community issue:
    https://github.com/coleam00/Archon/issues/1030).
    """
    while True:
        try:
            yield await asyncio.wait_for(response_gen.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return


def _get_claude_session(settings: Settings, porto_session_id: str) -> str | None:
    """Read porto→claude session mapping. Memory cache first, then sqlite."""
    if porto_session_id in _claude_session_map:
        return _claude_session_map[porto_session_id]
    try:
        with sqlite3.connect(str(settings.memory_db_path)) as conn:
            row = conn.execute(
                "SELECT claude_session_id FROM session_metadata WHERE session_id=?",
                (porto_session_id,),
            ).fetchone()
            if row:
                _claude_session_map[porto_session_id] = row[0]
                return row[0]
    except Exception:
        pass
    return None


def _set_claude_session(settings: Settings, porto_session_id: str, claude_session_id: str) -> None:
    """Persist porto→claude session mapping to memory cache + sqlite."""
    _claude_session_map[porto_session_id] = claude_session_id
    try:
        with sqlite3.connect(str(settings.memory_db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS session_metadata ("
                "  session_id TEXT PRIMARY KEY,"
                "  claude_session_id TEXT,"
                "  updated_at TEXT"
                ")"
            )
            conn.execute(
                "INSERT INTO session_metadata (session_id, claude_session_id, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "  claude_session_id=excluded.claude_session_id, "
                "  updated_at=excluded.updated_at",
                (porto_session_id, claude_session_id, datetime.now(UTC).isoformat()),
            )
    except Exception:
        pass


class AgentSDKBackend:
    """Claude Agent SDK engine.

    Uses ``ClaudeSDKClient`` + custom ``@tools`` + ``setting_sources`` for
    skill discovery. ``chat``/``chat_stream`` are stubbed and implemented in
    Task 7.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("backend.agent_sdk", settings)
        # Claude Code CLI (bundled in claude-agent-sdk) authenticates via
        # ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL env vars or ~/.claude/ OAuth
        # profile. Map Porto's langchain-oriented settings so the CLI subprocess
        # inherits them — users who already configured API key in Settings get
        # seamless auth without a separate `claude /login`.
        if settings.agent_api_key:
            os.environ["ANTHROPIC_API_KEY"] = settings.agent_api_key
        if settings.agent_base_url:
            os.environ["ANTHROPIC_BASE_URL"] = settings.agent_base_url

    def build_tools(self, ctx: AgentToolContext) -> list:
        """Return the SDK ``@tool`` list bound to ``ctx``.

        Returns ``[]`` when ``claude_agent_sdk`` is not installed — callers
        can detect this and fall back to another backend.
        """
        return build_sdk_tools(ctx, tool_timeout=self.settings.agent_tool_timeout)

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
                        if msg.subtype != ClaudeMsgSubtype.SUCCESS:
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
    def _capture_session(
        self,
        settings: Settings,
        porto_sid: str,
        returned_sid: str | None,
        expected_sid: str | None,
    ) -> None:
        """Record the Claude Code session_id returned by the SDK at init.

        When ``expected_sid`` was passed to ``resume`` but the SDK returns a
        different session_id, the resume likely failed silently (upstream
        issue #555) and prior conversation context may be lost. Log a warning
        so the break is observable in monitoring rather than swallowed.
        """
        if not returned_sid:
            return
        if expected_sid and returned_sid != expected_sid:
            self.logger.warning(
                "agent_sdk resume mismatch porto=%s expected=%s got=%s — "
                "resume may have silently created a new session, "
                "conversation context may be lost",
                porto_sid, expected_sid, returned_sid,
            )
        _set_claude_session(settings, porto_sid, returned_sid)
        self.logger.info(
            "agent_sdk session captured porto=%s claude=%s",
            porto_sid, returned_sid,
        )

    def _build_chat_options(
        self, req: ChatRequest, settings: Settings
    ) -> tuple[Any, dict[str, str], AgentToolContext, str | None]:
        """Build ``ClaudeAgentOptions`` + mutable state for chat/chat_stream.

        Returns ``(options, state, ctx, resume_sid)`` where ``state['answer_text']``
        is updated by the caller as ``AssistantMessage`` TextBlocks arrive, and
        ``resume_sid`` is the Claude Code session_id passed to ``resume`` (``None``
        for a fresh session). Callers compare it against the session_id returned
        at init to detect silent resume failures (see ``_capture_session``).

        The Stop hook closure reads ``state['answer_text']`` to persist the full
        conversation turn to :class:`MemoryStore` and trigger facts extraction.

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
        sdk_tools = build_sdk_tools(ctx, tool_timeout=settings.agent_tool_timeout)
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

        options_kwargs: dict[str, Any] = dict(
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
        # Session resume: if we have a Claude Code session_id for this Porto
        # session, pass it as `resume` so the CLI restores full conversation
        # context (including auto-compaction) from its session store.
        existing_claude_sid = _get_claude_session(settings, req.session_id)
        if existing_claude_sid:
            options_kwargs["resume"] = existing_claude_sid
            self.logger.info(
                "agent_sdk resume session porto=%s claude=%s",
                req.session_id, existing_claude_sid,
            )
        options = ClaudeAgentOptions(**options_kwargs)
        return options, state, ctx, existing_claude_sid

    async def _consume_chat_messages(
        self,
        req: ChatRequest,
        settings: Settings,
        options: Any,
        state: dict[str, Any],
        ctx: AgentToolContext,
        expected_sid: str | None,
    ) -> None:
        """Drive one ClaudeSDKClient turn for :meth:`chat`.

        Iterates ``receive_response()`` and mutates the shared ``state`` dict:
        accumulates ``AssistantMessage`` TextBlock content into
        ``state['answer_text']`` and appends tool-call entries to
        ``state['tool_steps']``. ``ctx`` is updated in place by the SDK tools.
        Any exception propagates to :meth:`chat` for error-response handling
        — keeping the try/except boundary identical to the pre-split code.
        """
        async with ClaudeSDKClient(options=options) as client:
            await client.query(req.message)
            async for msg in _receive_with_idle_timeout(
                client.receive_response(), settings.agent_sdk_idle_timeout,
            ):
                # Capture Claude Code session_id from init SystemMessage
                if SystemMessage is not None and isinstance(msg, SystemMessage):
                    if msg.subtype == ClaudeMsgSubtype.INIT:
                        self._capture_session(
                            settings, req.session_id,
                            msg.data.get("session_id"), expected_sid,
                        )
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            state["answer_text"] += block.text
                        elif isinstance(block, ToolUseBlock):
                            state.setdefault("tool_steps", []).append({
                                "name": f"tool:{block.name}",
                                "status": StepStatus.COMPLETED.value,
                                "summary": f"Claude 调用了 {block.name}",
                                "data": {"arguments": block.input},
                            })

    def _assemble_chat_response(
        self,
        req: ChatRequest,
        state: dict[str, Any],
        ctx: AgentToolContext,
    ) -> ChatResponse:
        """Build the success :class:`ChatResponse` after message consumption.

        Reads ``state['answer_text']`` (with placeholder fallback) and the
        tool-populated ``ctx.state`` to assemble the final answer, sources,
        memory, evaluation and step list. Evaluation is computed here (not in
        the message loop) so a missing answer still gets a placeholder score.
        """
        answer_text = state["answer_text"] or "（Agent 未返回内容，请重试或检查配置）"
        sources = ctx.state.get("tool_sources", [])
        memories = ctx.state.get("tool_memory", [])
        tool_steps = state.get("tool_steps", [])
        from ..evaluation import evaluate_rag_cases
        from ..models import EvalCase
        evaluation = evaluate_rag_cases([
            EvalCase(question=req.message, answer=answer_text,
                     contexts=[s.text for s in sources])
        ]).model_dump() if sources else {"score": 0.0, "passed": True, "cases": []}
        return ChatResponse(
            answer=answer_text,
            sources=sources,
            memory=memories,
            evaluation=evaluation,
            steps=tool_steps + [
                {
                    "name": "answer",
                    "status": StepStatus.COMPLETED.value,
                    "summary": "完成回答生成",
                    "data": {},
                },
            ] + ([{
                "name": "evaluate_rag",
                "status": StepStatus.COMPLETED.value,
                "summary": f"RAG eval score {evaluation['score']}",
                "data": evaluation,
            }] if sources else []),
        )

    async def chat(self, req: ChatRequest, settings: Settings) -> ChatResponse:
        """Chatbot-mode entry — Claude Agent SDK ReAct loop.

        No intent routing: Claude autonomously decides which tools to call
        (search_knowledgebase, search_memory, get_session_facts…). The Stop
        hook persists the conversation turn and triggers facts extraction.
        SDK failures degrade to a :class:`ChatResponse` with an error message
        instead of raising (callers get a user-friendly answer, never a 500).

        Structure: SDK-presence guard → RAG-availability guard →
        ``_consume_chat_messages`` (message loop mutating shared state) →
        ``_assemble_chat_response``. Exceptions from the message loop are
        caught here and converted to an error ChatResponse.
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
                        "status": StepStatus.FAILED.value,
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
                        "status": StepStatus.COMPLETED.value,
                        "summary": f"rag unavailable: {reason}",
                        "data": {"reason": reason},
                    },
                ],
            )

        try:
            options, state, ctx, expected_sid = self._build_chat_options(req, settings)
            await self._consume_chat_messages(
                req, settings, options, state, ctx, expected_sid,
            )
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                self.logger.warning(
                    "agent_sdk chat idle timeout session=%s timeout=%ss",
                    req.session_id, settings.agent_sdk_idle_timeout,
                )
                error_msg = "Claude 响应超时（长时间无输出），请重试或切换到 Langchain 引擎。"
            else:
                self.logger.exception(
                    "agent_sdk chat failed session=%s", req.session_id,
                )
                error_msg = f"Agent 引擎暂时不可用：{exc}。请在 Settings 切换到 Langchain 引擎。"
            return ChatResponse(
                answer=error_msg,
                sources=[],
                memory=[],
                evaluation={"score": 0.0, "passed": False, "cases": []},
                steps=[
                    {
                        "name": "agent_react",
                        "status": StepStatus.FAILED.value,
                        "summary": str(exc),
                        "data": {},
                    },
                ],
            )

        return self._assemble_chat_response(req, state, ctx)

    async def _stream_chat_messages(
        self,
        req: ChatRequest,
        settings: Settings,
        options: Any,
        state: dict[str, Any],
        ctx: AgentToolContext,
        expected_sid: str | None,
        text_id: str,
    ) -> AsyncIterator[str]:
        """Drive the SDK message loop for :meth:`chat_stream`, yielding SSE.

        Emits ``text-delta`` events as ``StreamEvent`` token deltas or
        ``AssistantMessage`` TextBlocks arrive. The local ``streamed`` flag
        mirrors the pre-split semantics: once any ``StreamEvent`` delta has
        been emitted, subsequent ``AssistantMessage`` TextBlocks are skipped
        (Claude sends the same content either as a stream or as a final
        assistant message, not both). Tool-use blocks populate
        ``state['tool_steps']`` for the Inspector panel.

        Exceptions propagate to :meth:`chat_stream`'s try/except for SSE
        error-termination handling.
        """
        from ..api.sse import _ai_sdk_sse

        streamed = False  # True once we emit via StreamEvent deltas
        async with ClaudeSDKClient(options=options) as client:
            await client.query(req.message)
            async for msg in _receive_with_idle_timeout(
                client.receive_response(), settings.agent_sdk_idle_timeout,
            ):
                # Capture Claude Code session_id from init SystemMessage
                if SystemMessage is not None and isinstance(msg, SystemMessage):
                    if msg.subtype == ClaudeMsgSubtype.INIT:
                        self._capture_session(
                            settings, req.session_id,
                            msg.data.get("session_id"), expected_sid,
                        )
                # Token-level streaming: StreamEvent carries content_block_delta
                elif StreamEvent is not None and isinstance(msg, StreamEvent):
                    event = msg.event
                    if (
                        event.get("type") == AnthropicEventType.CONTENT_BLOCK_DELTA
                        and event.get("delta", {}).get("type") == AnthropicDeltaType.TEXT_DELTA
                    ):
                        delta_text = event["delta"].get("text", "")
                        if delta_text:
                            streamed = True
                            state["answer_text"] += delta_text
                            yield _ai_sdk_sse(
                                {
                                    "type": "text-delta",
                                    "id": text_id,
                                    "delta": delta_text,
                                },
                            )
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text and not streamed:
                            state["answer_text"] += block.text
                            yield _ai_sdk_sse(
                                {
                                    "type": "text-delta",
                                    "id": text_id,
                                    "delta": block.text,
                                },
                            )
                        elif isinstance(block, ToolUseBlock):
                            state.setdefault("tool_steps", []).append({
                                "name": f"tool:{block.name}",
                                "status": StepStatus.COMPLETED.value,
                                "summary": f"Claude 调用了 {block.name}",
                                "data": {"arguments": block.input},
                            })

    async def _emit_chat_stream_finalize(
        self,
        req: ChatRequest,
        state: dict[str, Any],
        ctx: AgentToolContext,
        text_id: str,
    ) -> AsyncIterator[str]:
        """Emit the post-message-loop SSE sequence for a successful stream.

        Yields ``text-end`` → Inspector ``data-porto`` (steps/sources/memory/
        evaluation) → ``finish-step`` → ``finish`` (stop) → ``[DONE]``.
        Evaluation is computed here (mirrors :meth:`_assemble_chat_response`)
        so the Inspector panel gets a score even for tool-sourced answers.
        """
        from ..api.sse import _ai_sdk_sse
        from ..evaluation import evaluate_rag_cases
        from ..models import EvalCase

        yield _ai_sdk_sse({"type": "text-end", "id": text_id})

        # Extract tool-collected data from ctx.state for Inspector panel
        sources = ctx.state.get("tool_sources", [])
        memories = ctx.state.get("tool_memory", [])
        tool_steps = state.get("tool_steps", [])
        evaluation = evaluate_rag_cases([
            EvalCase(question=req.message, answer=state["answer_text"],
                     contexts=[s.text for s in sources])
        ]).model_dump() if sources else {"score": 0.0, "passed": True, "cases": []}
        inspector_steps = tool_steps + [
            {"name": "answer", "status": StepStatus.COMPLETED.value,
             "summary": "完成回答生成", "data": {}},
        ]
        if sources:
            inspector_steps.append({
                "name": "evaluate_rag", "status": StepStatus.COMPLETED.value,
                "summary": f"RAG eval score {evaluation['score']}",
                "data": evaluation,
            })

        yield _ai_sdk_sse(
            {
                "type": "data-porto",
                "id": "porto-inspector",
                "transient": True,
                "data": {
                    "steps": inspector_steps,
                    "sources": [s.model_dump() for s in sources],
                    "memory": [m.model_dump() for m in memories],
                    "evaluation": evaluation,
                    "workflow": None,
                },
            }
        )
        yield _ai_sdk_sse({"type": "finish-step"})
        yield _ai_sdk_sse(
            {
                "type": "finish",
                "finishReason": "stop",
                "messageMetadata": {"source_count": 0},
            },
        )
        yield "data: [DONE]\n\n"

    async def _stream_chat_unavailable_hint(
        self, req: ChatRequest, reason: str | None, text_id: str
    ) -> AsyncIterator[str]:
        """Emit the RAG-unavailable SSE sequence for :meth:`chat_stream`.

        Yields the full start → text deltas (sliced hint) → finish → [DONE]
        sequence so the frontend stream terminates cleanly when the index is
        unavailable. Mirrors the inline block that previously lived in
        :meth:`chat_stream`.
        """
        from ..api.sse import _ai_sdk_sse, _text_chunks

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

    async def chat_stream(
        self, req: ChatRequest, settings: Settings
    ) -> AsyncIterator[str]:
        """SSE streaming chatbot entry — wraps the SDK message stream.

        Emits ai-sdk SSE events (start → start-step → text-start →
        text-delta(s) → text-end → finish-step → finish → [DONE]) mirroring
        :func:`langchain_chat_stream`. RAG-unavailable and SDK-error paths
        emit a proper SSE error/finish sequence so the frontend stream always
        terminates cleanly.

        Structure: RAG guard (delegates to ``_stream_chat_unavailable_hint``)
        → SDK guard (inline error SSE) → emit start sequence →
        ``_stream_chat_messages`` (token/message loop, yields text-delta) →
        ``_emit_chat_stream_finalize`` (Inspector + finish). Sub-generator
        exceptions propagate to the try/except here for SSE error termination.
        """
        from ..api.deps import get_index_supervisor
        from ..api.sse import _ai_sdk_sse

        text_id = "answer-1"

        # -- RAG availability check (mirror chat's early return) -------------
        available, reason = get_index_supervisor().rag_available()
        if not available:
            async for chunk in self._stream_chat_unavailable_hint(req, reason, text_id):
                yield chunk
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
            options, state, ctx, expected_sid = self._build_chat_options(req, settings)
            async for chunk in self._stream_chat_messages(
                req, settings, options, state, ctx, expected_sid, text_id,
            ):
                yield chunk
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                self.logger.warning(
                    "agent_sdk chat_stream idle timeout session=%s timeout=%ss",
                    req.session_id, settings.agent_sdk_idle_timeout,
                )
                error_text = "Claude 响应超时（长时间无输出），请重试或切换到 Langchain 引擎。"
            else:
                self.logger.exception(
                    "agent_sdk chat_stream failed session=%s", req.session_id,
                )
                error_text = str(exc)
            yield _ai_sdk_sse({"type": "error", "errorText": error_text})
            yield _ai_sdk_sse({"type": "text-end", "id": text_id})
            yield _ai_sdk_sse({"type": "finish-step"})
            yield _ai_sdk_sse({"type": "finish", "finishReason": "error"})
            yield "data: [DONE]\n\n"
            return

        async for chunk in self._emit_chat_stream_finalize(req, state, ctx, text_id):
            yield chunk
