# backend/tests/test_sdk_tools.py
"""Test that build_sdk_tools wraps existing handlers correctly.

The @tool-decorated functions bind an AgentToolContext via closure and delegate
to handlers.py — zero duplication. Tests cover:
- Workflow-only context (6 core tools)
- Chatbot context (adds search_memory + get_session_facts)
- Tool handler invocation actually calls the underlying handler.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from porto_chatbot.tools.context import AgentToolContext


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Import build_sdk_tools lazily so the test module can still be collected when
# the SDK is missing — individual tests skip instead.
# --------------------------------------------------------------------------- #
def _import_build_sdk_tools():
    from porto_chatbot.agent_sdk.tools import build_sdk_tools
    return build_sdk_tools


def _sdk_available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark = pytest.mark.skipif(not _sdk_available(), reason="claude-agent-sdk not installed")


# --------------------------------------------------------------------------- #
# Workflow-only context
# --------------------------------------------------------------------------- #
def test_build_sdk_tools_returns_list():
    ctx = AgentToolContext(state={"prd_text": "test PRD"})
    build_sdk_tools = _import_build_sdk_tools()
    tools = build_sdk_tools(ctx)
    assert isinstance(tools, list)
    assert len(tools) >= 4  # at least get_prd, search_kb, get_understanding, etc.


def test_build_sdk_tools_has_six_workflow_tools():
    """Workflow context (no memory_store) yields exactly 6 tools."""
    ctx = AgentToolContext(state={"prd_text": "test PRD"})
    build_sdk_tools = _import_build_sdk_tools()
    tools = build_sdk_tools(ctx)

    names = sorted(t.name for t in tools)
    assert names == [
        "get_prd_text",
        "get_sources",
        "get_subsystem",
        "get_understanding",
        "list_subsystems",
        "search_knowledgebase",
    ]


def test_build_sdk_tools_no_chatbot_tools_when_memory_absent():
    ctx = AgentToolContext(state={"prd_text": "test"})
    build_sdk_tools = _import_build_sdk_tools()
    tools = build_sdk_tools(ctx)
    names = [t.name for t in tools]
    assert not any("memory" in n.lower() for n in names)
    assert not any("fact" in n.lower() for n in names)


# --------------------------------------------------------------------------- #
# Chatbot context (memory_store + facts_store present)
# --------------------------------------------------------------------------- #
def test_build_sdk_tools_includes_chatbot_tools_when_memory_present():
    """When ctx has memory_store, chatbot-specific tools are registered."""
    ctx = AgentToolContext(
        state={"prd_text": "test"},
        memory_store=MagicMock(),
        facts_store=MagicMock(),
    )
    build_sdk_tools = _import_build_sdk_tools()
    tools = build_sdk_tools(ctx)
    names = [t.name for t in tools]
    assert "search_memory" in names
    assert "get_session_facts" in names


def test_build_sdk_tools_only_memory_store_adds_search_memory():
    """Having only memory_store (no facts_store) registers just search_memory."""
    ctx = AgentToolContext(state={}, memory_store=MagicMock(), facts_store=None)
    build_sdk_tools = _import_build_sdk_tools()
    tools = build_sdk_tools(ctx)
    names = [t.name for t in tools]
    assert "search_memory" in names
    assert "get_session_facts" not in names


def test_build_sdk_tools_only_facts_store_adds_get_session_facts():
    ctx = AgentToolContext(state={}, memory_store=None, facts_store=MagicMock())
    build_sdk_tools = _import_build_sdk_tools()
    tools = build_sdk_tools(ctx)
    names = [t.name for t in tools]
    assert "get_session_facts" in names
    assert "search_memory" not in names


# --------------------------------------------------------------------------- #
# Tool handlers actually invoke the underlying handlers.py functions
# --------------------------------------------------------------------------- #
def test_get_prd_text_tool_returns_prd():
    """Invoking the wrapped tool's handler returns the PRD text via closure."""
    ctx = AgentToolContext(state={"prd_text": "my fancy PRD"})
    build_sdk_tools = _import_build_sdk_tools()
    tools = build_sdk_tools(ctx)
    prd_tool = next(t for t in tools if t.name == "get_prd_text")

    async def _call():
        return await prd_tool.handler({})

    result = _run(_call())
    assert result["content"][0]["type"] == "text"
    assert "my fancy PRD" in result["content"][0]["text"]


def test_get_subsystem_tool_passes_name_argument():
    """get_subsystem tool forwards the `name` arg to _get_subsystem."""
    ctx = AgentToolContext(
        state={
            "subsystems": [
                {"name": "auth", "responsibility": "logins",
                 "capabilities": [], "data_entities": [], "dependencies": [],
                 "type": "core"},
            ],
        },
    )
    build_sdk_tools = _import_build_sdk_tools()
    tools = build_sdk_tools(ctx)
    sub_tool = next(t for t in tools if t.name == "get_subsystem")

    async def _call():
        return await sub_tool.handler({"name": "auth"})

    text = _run(_call())["content"][0]["text"]
    assert "auth" in text
    assert "logins" in text


def test_search_knowledgebase_tool_returns_unavailable_without_store():
    """Without vector_store, the handler returns the 'unavailable' message."""
    ctx = AgentToolContext(state={}, vector_store=None)
    build_sdk_tools = _import_build_sdk_tools()
    tools = build_sdk_tools(ctx)
    kb_tool = next(t for t in tools if t.name == "search_knowledgebase")

    async def _call():
        return await kb_tool.handler({"query": "payments", "top_k": 3})

    result = _run(_call())
    assert "不可用" in result["content"][0]["text"]


def test_search_memory_tool_calls_memory_store():
    """search_memory delegates to ctx.memory_store.search with kwargs."""
    fake_store = MagicMock()
    fake_store.search.return_value = []  # empty → "无匹配记忆"
    ctx = AgentToolContext(state={}, memory_store=fake_store)
    build_sdk_tools = _import_build_sdk_tools()
    tools = build_sdk_tools(ctx)
    mem_tool = next(t for t in tools if t.name == "search_memory")

    async def _call():
        return await mem_tool.handler({"query": "x", "session_id": "sess-1"})

    result = _run(_call())
    fake_store.search.assert_called_once_with("x", session_id="sess-1")
    assert "无匹配记忆" in result["content"][0]["text"]


def test_get_session_facts_tool_uses_build_facts_prompt():
    """get_session_facts calls facts_store.by_category and build_facts_prompt."""
    from porto_chatbot.memory.facts import build_facts_prompt

    fake_store = MagicMock()
    fake_store.by_category.return_value = {}  # empty → empty prompt → fallback msg
    ctx = AgentToolContext(state={}, facts_store=fake_store)
    build_sdk_tools = _import_build_sdk_tools()
    tools = build_sdk_tools(ctx)
    facts_tool = next(t for t in tools if t.name == "get_session_facts")

    async def _call():
        return await facts_tool.handler({"session_id": "s2"})

    result = _run(_call())
    fake_store.by_category.assert_called_once_with("s2")
    # build_facts_prompt({}) returns "" → fallback message
    assert build_facts_prompt({}) == ""
    assert "无结构化事实" in result["content"][0]["text"]
