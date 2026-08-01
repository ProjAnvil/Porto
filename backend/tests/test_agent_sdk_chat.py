# backend/tests/test_agent_sdk_chat.py
"""Test AgentSDKBackend.chat() and chat_stream() (Task 7).

The real ClaudeSDKClient spawns the Claude Code CLI subprocess — we mock it so
tests stay deterministic and offline. ``get_index_supervisor`` is also mocked
to control RAG availability without touching real indices.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from porto_chatbot.agent_sdk.backend import AgentSDKBackend
from porto_chatbot.models import ChatRequest
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
def _make_backend(tmp_path) -> tuple[AgentSDKBackend, Settings]:
    """Construct AgentSDKBackend with isolated tmp_path settings."""
    s = Settings(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        embedding_provider="local",
        embedding_dimensions=128,
    )
    return AgentSDKBackend(s), s


def _build_assistant_text(text: str):
    """Build a fake AssistantMessage containing a single TextBlock."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    return AssistantMessage(content=[TextBlock(text=text)], model="claude-test")


class _FakeClient:
    """Async-context-manager fake that records the query and yields messages."""

    def __init__(self, messages):
        self._messages = messages
        self.query_prompt: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def query(self, prompt):
        self.query_prompt = prompt

    async def receive_response(self):
        for msg in self._messages:
            yield msg


class _ExplodingClient:
    """Async CM that raises inside __aenter__."""

    async def __aenter__(self):
        raise RuntimeError("SDK unavailable")

    async def __aexit__(self, *args):
        return False


def _rag_available_mock(available: bool = True, reason: str | None = None):
    """Build a mock for ``get_index_supervisor()`` chain."""
    supervisor = MagicMock()
    supervisor.rag_available.return_value = (available, reason)
    return supervisor


def _patch_rag(available: bool = True, reason: str | None = None):
    """Context manager patching ``get_index_supervisor`` in deps module."""
    return patch(
        "porto_chatbot.api.deps.get_index_supervisor",
        return_value=_rag_available_mock(available, reason),
    )


def _patch_mcp_server():
    """Patch ``create_sdk_mcp_server`` to return a MagicMock (no real MCP)."""
    return patch(
        "porto_chatbot.agent_sdk.backend.create_sdk_mcp_server",
        return_value=MagicMock(name="fake_server"),
    )


# --------------------------------------------------------------------------- #
# chat() — RAG gating
# --------------------------------------------------------------------------- #
def test_chat_returns_unavailable_when_rag_down(tmp_path):
    """When RAG is unavailable, chat() returns a ChatResponse with the hint."""
    backend, s = _make_backend(tmp_path)
    req = ChatRequest(message="hi", session_id="test-session")

    with _patch_rag(available=False, reason="index_unavailable"), \
         _patch_mcp_server():
        result = asyncio.run(backend.chat(req, s))

    assert "不可用" in result.answer
    assert result.sources == []
    assert result.steps[0].name == "rag_check"
    assert result.steps[0].status == "completed"
    # Must not have called the SDK client at all.
    assert result.evaluation["passed"] is False


# --------------------------------------------------------------------------- #
# chat() — SDK failure degrades to error ChatResponse
# --------------------------------------------------------------------------- #
def test_chat_returns_error_on_sdk_failure(tmp_path):
    """When ClaudeSDKClient raises, chat() returns error info (not crash/500)."""
    backend, s = _make_backend(tmp_path)
    req = ChatRequest(message="hi", session_id="test-session")

    with _patch_rag(available=True), \
         _patch_mcp_server(), \
         patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient",
               return_value=_ExplodingClient()):
        result = asyncio.run(backend.chat(req, s))

    assert result.answer is not None
    assert "不可用" in result.answer or "失败" in result.answer or "暂时不可用" in result.answer
    assert result.steps[-1].status == "failed"
    assert "SDK unavailable" in result.steps[-1].summary


# --------------------------------------------------------------------------- #
# chat() — success path
# --------------------------------------------------------------------------- #
def test_chat_returns_answer_on_success(tmp_path):
    """AssistantMessage TextBlock content is returned as the answer."""
    backend, s = _make_backend(tmp_path)
    req = ChatRequest(message="hello?", session_id="sess-1")
    fake = _FakeClient([
        _build_assistant_text("Porto "),
        _build_assistant_text("rocks!"),
    ])

    with _patch_rag(available=True), \
         _patch_mcp_server(), \
         patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient", return_value=fake):
        result = asyncio.run(backend.chat(req, s))

    assert result.answer == "Porto rocks!"
    assert result.evaluation["passed"] is True
    assert result.steps[-1].name == "answer"
    assert result.steps[-1].status == "completed"
    assert fake.query_prompt == "hello?"


def test_chat_returns_placeholder_when_no_text(tmp_path):
    """When the SDK yields no AssistantMessage text, a placeholder is returned."""
    backend, s = _make_backend(tmp_path)
    req = ChatRequest(message="hi", session_id="sess-empty")
    fake = _FakeClient([])  # no messages at all

    with _patch_rag(available=True), \
         _patch_mcp_server(), \
         patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient", return_value=fake):
        result = asyncio.run(backend.chat(req, s))

    assert "未返回内容" in result.answer


# --------------------------------------------------------------------------- #
# chat_stream() — SSE events
# --------------------------------------------------------------------------- #
def _consume_stream(coro):
    """Run an async generator to completion, collecting all yielded strings."""
    async def _collect():
        chunks: list[str] = []
        async for chunk in coro:
            chunks.append(chunk)
        return chunks
    return asyncio.run(_collect())


def _parse_sse_events(chunks: list[str]) -> list[dict]:
    """Parse ``data: {...}\\n\\n`` chunks into a list of event dicts."""
    events: list[dict] = []
    for chunk in chunks:
        text = chunk.strip()
        if not text.startswith("data: "):
            continue
        payload = text[len("data: "):]
        if payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    return events


def test_chat_stream_yields_text_deltas_on_success(tmp_path):
    """Stream emits start → text-delta(s) → finish sequence."""
    backend, s = _make_backend(tmp_path)
    req = ChatRequest(message="hello", session_id="stream-1")
    fake = _FakeClient([_build_assistant_text("world")])

    with _patch_rag(available=True), \
         _patch_mcp_server(), \
         patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient", return_value=fake):
        chunks = _consume_stream(backend.chat_stream(req, s))

    events = _parse_sse_events(chunks)
    types = [e["type"] for e in events]
    assert "start" in types
    assert "text-start" in types
    assert "text-delta" in types
    assert "text-end" in types
    assert "finish" in types
    assert types[-1] == "finish"

    # The text-delta should carry the AssistantMessage text.
    deltas = [e["delta"] for e in events if e["type"] == "text-delta"]
    assert "".join(deltas) == "world"

    # The stream must end with [DONE].
    assert chunks[-1].strip() == "data: [DONE]"

    # The finish event should report "stop" (not "error").
    finish_event = [e for e in events if e["type"] == "finish"][0]
    assert finish_event["finishReason"] == "stop"


def test_chat_stream_yields_error_on_sdk_failure(tmp_path):
    """When the SDK raises mid-stream, an error event + finish='error' is emitted."""
    backend, s = _make_backend(tmp_path)
    req = ChatRequest(message="hi", session_id="stream-err")

    with _patch_rag(available=True), \
         _patch_mcp_server(), \
         patch("porto_chatbot.agent_sdk.backend.ClaudeSDKClient",
               return_value=_ExplodingClient()):
        chunks = _consume_stream(backend.chat_stream(req, s))

    events = _parse_sse_events(chunks)
    types = [e["type"] for e in events]
    assert "error" in types
    assert types[-1] == "finish"
    finish = [e for e in events if e["type"] == "finish"][0]
    assert finish["finishReason"] == "error"
    assert chunks[-1].strip() == "data: [DONE]"


def test_chat_stream_yields_rag_unavailable_hint(tmp_path):
    """When RAG is down, the stream emits the hint and terminates cleanly."""
    backend, s = _make_backend(tmp_path)
    req = ChatRequest(message="hi", session_id="stream-rag")

    with _patch_rag(available=False, reason="index_unavailable"):
        chunks = _consume_stream(backend.chat_stream(req, s))

    events = _parse_sse_events(chunks)
    types = [e["type"] for e in events]
    assert "text-delta" in types
    deltas = "".join(e["delta"] for e in events if e["type"] == "text-delta")
    assert "不可用" in deltas
    assert types[-1] == "finish"
    assert chunks[-1].strip() == "data: [DONE]"


# --------------------------------------------------------------------------- #
# Stop hook — memory persistence + facts trigger
# --------------------------------------------------------------------------- #
def test_stop_hook_persists_conversation(tmp_path):
    """The Stop hook persists user+assistant messages to MemoryStore."""
    backend, s = _make_backend(tmp_path)
    req = ChatRequest(message="user question", session_id="hook-sess")

    with _patch_rag(available=True), \
         _patch_mcp_server():
        options, state, _ctx = backend._build_chat_options(req, s)

    # Extract the Stop hook callback from the options.
    stop_matchers = options.hooks["Stop"]
    hook_cb = stop_matchers[0].hooks[0]

    # Simulate that the SDK produced some answer text.
    state["answer_text"] = "assistant answer"

    # Invoke the hook (async). It should persist to real MemoryStore.
    asyncio.run(hook_cb(MagicMock(), None, MagicMock()))

    # Verify the conversation was persisted.
    from porto_chatbot.api.deps import get_memory

    memory = get_memory(s)
    records = memory.list_session("hook-sess")
    roles = [r.role for r in records]
    assert "user" in roles
    assert "assistant" in roles
    user_msg = next(r for r in records if r.role == "user")
    assert user_msg.content == "user question"
    asst_msg = next(r for r in records if r.role == "assistant")
    assert asst_msg.content == "assistant answer"


def test_stop_hook_swallows_memory_errors(tmp_path):
    """If MemoryStore.add raises, the Stop hook must not propagate."""
    backend, s = _make_backend(tmp_path)
    req = ChatRequest(message="q", session_id="err-sess")

    with _patch_rag(available=True), \
         _patch_mcp_server():
        options, state, _ctx = backend._build_chat_options(req, s)

    hook_cb = options.hooks["Stop"][0].hooks[0]
    state["answer_text"] = "answer"

    # Patch MemoryStore.add to raise — the hook must catch and return {}.
    with patch("porto_chatbot.memory.store.MemoryStore.add",
               side_effect=RuntimeError("db locked")):
        result = asyncio.run(hook_cb(MagicMock(), None, MagicMock()))

    assert result == {}  # no-op output, exception swallowed
