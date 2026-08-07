# backend/tests/test_agent_sdk_backend.py
"""Test AgentSDKBackend Protocol conformance + execute_node wiring.

The real ClaudeSDKClient spawns the Claude Code CLI subprocess — we mock it so
tests stay deterministic and offline.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from porto_chatbot.agent.backends import AgentBackend, NodeExecutionResult
from porto_chatbot.settings import Settings


def _sdk_available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark = pytest.mark.skipif(not _sdk_available(), reason="claude-agent-sdk not installed")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_backend(tmp_path):
    """Construct AgentSDKBackend with isolated settings."""
    from porto_chatbot.agent_sdk.backend import AgentSDKBackend

    s = Settings(data_dir=tmp_path / "data", log_dir=tmp_path / "logs")
    return AgentSDKBackend(s)


def _build_assistant_text(text: str):
    """Build a fake AssistantMessage containing a single TextBlock."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    return AssistantMessage(content=[TextBlock(text=text)], model="claude-test")


def _build_result_message(*, subtype: str = "success", num_turns: int = 1,
                          result: str | None = None, structured_output=None):
    """Build a fake ResultMessage with the requested fields."""
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype=subtype,
        duration_ms=10,
        duration_api_ms=10,
        is_error=False,
        num_turns=num_turns,
        session_id="test",
        result=result,
        structured_output=structured_output,
    )


class _FakeClient:
    """Async-context-manager fake that records the query and yields messages."""

    def __init__(self, messages):
        self._messages = messages
        self.query_prompt = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def query(self, prompt):
        self.query_prompt = prompt

    async def receive_response(self):
        for msg in self._messages:
            yield msg


# --------------------------------------------------------------------------- #
# Protocol conformance
# --------------------------------------------------------------------------- #
def test_agent_sdk_backend_satisfies_protocol(tmp_path):
    backend = _make_backend(tmp_path)
    assert isinstance(backend, AgentBackend)


def test_agent_sdk_backend_build_tools_returns_list(tmp_path):
    """build_tools delegates to build_sdk_tools, which returns a list."""
    from porto_chatbot.tools.context import AgentToolContext

    backend = _make_backend(tmp_path)
    ctx = AgentToolContext(state={"prd_text": "x"})
    tools = backend.build_tools(ctx)
    assert isinstance(tools, list)


# --------------------------------------------------------------------------- #
# execute_node wiring (mocked ClaudeSDKClient)
# --------------------------------------------------------------------------- #
def test_execute_node_collects_assistant_text(tmp_path):
    """AssistantMessage TextBlock content is concatenated into result.text."""
    backend = _make_backend(tmp_path)
    fake = _FakeClient([_build_assistant_text("hello "), _build_assistant_text("world"),
                        _build_result_message(num_turns=2)])

    with patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient", return_value=fake):
        result = asyncio.run(
            backend.execute_node(system="sys", user="hi", tools=None)
        )

    assert isinstance(result, NodeExecutionResult)
    assert result.text == "hello world"
    assert result.turns == 2
    assert result.truncated is False
    assert fake.query_prompt == "hi"


def test_execute_node_passes_structured_output_through(tmp_path):
    """When structured_schema is provided, options.output_format is set and
    ResultMessage.structured_output flows through to NodeExecutionResult.structured.
    """
    backend = _make_backend(tmp_path)
    fake = _FakeClient([
        _build_result_message(num_turns=1, structured_output={"subsystems": [{"name": "svc"}]}),
    ])

    captured_options = []

    with patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient", return_value=fake), \
         patch("porto_chatbot.agent_sdk.backend.ClaudeAgentOptions") as opts_cls:
        # Make the constructor a real recorder that just stores kwargs
        def _ctor(**kwargs):
            captured_options.append(kwargs)
            return MagicMock(**kwargs)
        opts_cls.side_effect = _ctor

        result = asyncio.run(
            backend.execute_node(
                system="sys", user="u",
                structured_schema={"type": "object"},
            )
        )

    assert result.structured == {"subsystems": [{"name": "svc"}]}
    assert len(captured_options) == 1
    assert captured_options[0]["output_format"] == {"type": "object"}
    assert captured_options[0]["system_prompt"] == "sys"


def test_execute_node_structured_falls_back_to_result_string(tmp_path):
    """If structured_output is None but result is a JSON string, parse it."""
    backend = _make_backend(tmp_path)
    payload = {"a": 1}
    fake = _FakeClient([
        _build_result_message(num_turns=1, result=json.dumps(payload),
                              structured_output=None),
    ])

    with patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient", return_value=fake):
        result = asyncio.run(
            backend.execute_node(system="s", user="u",
                                 structured_schema={"type": "object"})
        )

    assert result.structured == payload


def test_execute_node_flags_non_success_subtype(tmp_path):
    """A ResultMessage with subtype != 'success' marks the result truncated."""
    backend = _make_backend(tmp_path)
    fake = _FakeClient([_build_result_message(subtype="error_max_turns", num_turns=10)])

    with patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient", return_value=fake):
        result = asyncio.run(backend.execute_node(system="s", user="u"))

    assert result.truncated is True
    assert result.reason == "error_max_turns"


def test_execute_node_returns_error_result_on_exception(tmp_path):
    """An exception inside the SDK client must not crash — return a
    NodeExecutionResult with truncated=True and reason='agent_sdk_error'.
    """
    backend = _make_backend(tmp_path)

    class _ExplodingClient:
        async def __aenter__(self):
            raise RuntimeError("boom")

        async def __aexit__(self, *args):
            return False

    with patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient", return_value=_ExplodingClient()):
        result = asyncio.run(backend.execute_node(system="s", user="u"))

    assert isinstance(result, NodeExecutionResult)
    assert result.truncated is True
    assert result.reason == "agent_sdk_error"
    assert "Agent SDK" in result.text or "agent_sdk" in result.text.lower() or "boom" in result.text


def test_execute_node_passes_tools_as_mcp_server(tmp_path):
    """When tools list is non-empty, options.mcp_servers + allowed_tools set."""
    backend = _make_backend(tmp_path)
    fake = _FakeClient([_build_result_message()])

    captured: list[dict] = []

    with patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient", return_value=fake), \
         patch("porto_chatbot.agent_sdk.backend.ClaudeAgentOptions") as opts_cls, \
         patch("porto_chatbot.agent_sdk.backend.create_sdk_mcp_server") as mk_srv:
        mk_srv.return_value = MagicMock(name="server")
        opts_cls.side_effect = lambda **kw: captured.append(kw) or MagicMock(**kw)

        asyncio.run(
            backend.execute_node(system="s", user="u", tools=["fake_tool"])
        )

    assert len(captured) == 1
    kw = captured[0]
    assert "mcp_servers" in kw
    assert "allowed_tools" in kw
    assert kw["allowed_tools"] == ["mcp__porto__*"]
    mk_srv.assert_called_once()
    _, kwargs = mk_srv.call_args
    assert kwargs["name"] == "porto"


def test_execute_node_omits_mcp_when_tools_empty(tmp_path):
    """Empty tools list → no mcp_servers / no allowed_tools in options."""
    backend = _make_backend(tmp_path)
    fake = _FakeClient([_build_result_message()])

    captured: list[dict] = []

    with patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient", return_value=fake), \
         patch("porto_chatbot.agent_sdk.backend.ClaudeAgentOptions") as opts_cls:
        opts_cls.side_effect = lambda **kw: captured.append(kw) or MagicMock(**kw)

        asyncio.run(
            backend.execute_node(system="s", user="u", tools=[])
        )

    assert len(captured) == 1
    kw = captured[0]
    assert "mcp_servers" not in kw
    assert "allowed_tools" not in kw


# --------------------------------------------------------------------------- #
# Task 7 — decide_intent_from_tool_calls + AgentToolContext.session_id
# --------------------------------------------------------------------------- #
def test_decide_intent_from_tool_calls_with_rag():
    """RAG 工具调用 → intent='rag', index_vector=True。"""
    from collections import Counter

    from porto_chatbot.agent_sdk.backend import decide_intent_from_tool_calls

    tool_calls: Counter[tuple[str, str]] = Counter()
    tool_calls[("search_knowledgebase", '{"query":"payment"}')] += 1
    intent, index_vector = decide_intent_from_tool_calls(tool_calls)
    assert intent == "rag"
    assert index_vector is True


def test_decide_intent_from_tool_calls_without_rag():
    """无 RAG 工具调用 → intent='direct', index_vector=False。"""
    from collections import Counter

    from porto_chatbot.agent_sdk.backend import decide_intent_from_tool_calls

    tool_calls: Counter[tuple[str, str]] = Counter()
    tool_calls[("get_prd_text", '{}')] += 1
    intent, index_vector = decide_intent_from_tool_calls(tool_calls)
    assert intent == "direct"
    assert index_vector is False


def test_decide_intent_search_memory_counts_as_rag():
    """search_memory 也算 RAG 工具。"""
    from collections import Counter

    from porto_chatbot.agent_sdk.backend import decide_intent_from_tool_calls

    tool_calls: Counter[tuple[str, str]] = Counter()
    tool_calls[("search_memory", '{"query":"history"}')] += 1
    intent, _ = decide_intent_from_tool_calls(tool_calls)
    assert intent == "rag"


def test_agent_tool_context_has_session_id():
    from porto_chatbot.tools.context import AgentToolContext

    ctx = AgentToolContext(state={}, session_id="test-sid")
    assert ctx.session_id == "test-sid"
    # Default is None for workflow mode
    ctx2 = AgentToolContext(state={})
    assert ctx2.session_id is None


# --------------------------------------------------------------------------- #
# chat / chat_stream — implemented in Task 7 (see test_agent_sdk_chat.py)
# --------------------------------------------------------------------------- #
