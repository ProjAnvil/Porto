"""Verify that nodes use backend.execute_node instead of direct LLM calls.

These are mock-based tests: we verify the dispatch goes through backend,
not that the LLM output is correct (existing tests cover that).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from porto_chatbot.agent.backends import NodeExecutionResult
from porto_chatbot.models import AgentStep


# ----------------------------- helpers ----------------------------- #


def _make_agent(*, llm_enabled: bool = True) -> MagicMock:
    """Lightweight stand-in agent with backend mocked.

    Mirrors the pattern in test_understand_node_truncation.py but mocks at the
    backend layer (the new dispatch point) instead of the LLM layer.
    """
    agent = MagicMock()
    agent.llm.enabled = llm_enabled
    agent.settings.agent_max_tool_turns = 10
    agent.vector_store = MagicMock()
    agent.critic_llm = MagicMock()
    agent.critic_llm.enabled = True
    agent._step = lambda name, summary, data: {"steps": [AgentStep(
        name=name, status="completed", summary=summary, data=data)]}
    return agent


# ----------------------------- understand_prd ----------------------------- #


def test_understand_uses_backend():
    """understand_prd calls agent.backend.execute_node, not agent.llm.complete_with_tools."""
    from porto_chatbot.agent.nodes.understand import understand_prd

    agent = _make_agent()
    mock_result = NodeExecutionResult(text="mocked understanding", turns=2)
    agent.backend.execute_node = AsyncMock(return_value=mock_result)

    state = {"workflow_id": "w1", "prd_text": "test PRD", "sources": []}
    result = understand_prd(state, config={"configurable": {"agent": agent}})
    assert result["understanding"] == "mocked understanding"
    agent.backend.execute_node.assert_called_once()

    # Verify tools mode (tools= keyword passed)
    _, kwargs = agent.backend.execute_node.call_args
    assert kwargs.get("tools") is not None


def test_understand_llm_disabled_skips_backend():
    """When llm.enabled=False, understand_prd uses fallback and does NOT touch backend."""
    from porto_chatbot.agent.nodes.understand import understand_prd

    agent = _make_agent(llm_enabled=False)
    agent.backend.execute_node = AsyncMock()

    state = {"workflow_id": "w1", "prd_text": "需要一个订单管理模块", "sources": []}
    result = understand_prd(state, config={"configurable": {"agent": agent}})
    assert result["understanding"]  # non-empty fallback
    agent.backend.execute_node.assert_not_called()


# ----------------------------- identify_subsystems ----------------------------- #


def test_identify_uses_backend_structured():
    """identify_subsystems calls agent.backend.execute_node with structured_schema."""
    from porto_chatbot.agent.nodes.identify import identify_subsystems

    agent = _make_agent()
    mock_result = NodeExecutionResult(
        structured={
            "subsystems": [
                {
                    "name": "payment-service",
                    "type": "new",
                    "responsibility": "payments",
                    "capabilities": [],
                    "data_entities": [],
                    "dependencies": [],
                }
            ]
        }
    )
    agent.backend.execute_node = AsyncMock(return_value=mock_result)

    state = {
        "workflow_id": "w1",
        "prd_text": "test",
        "understanding": "test understanding",
        "sources": [],
    }
    result = identify_subsystems(state, config={"configurable": {"agent": agent}})
    assert len(result["subsystems"]) == 1
    assert result["subsystems"][0].name == "payment-service"
    agent.backend.execute_node.assert_called_once()

    _, kwargs = agent.backend.execute_node.call_args
    assert kwargs.get("structured_schema") is not None
    assert kwargs.get("tools") is None  # structured mode, not tools mode


def test_identify_llm_disabled_uses_fallback():
    """When llm.disabled, identify falls back to heuristics without touching backend."""
    from porto_chatbot.agent.nodes.identify import identify_subsystems

    agent = _make_agent(llm_enabled=False)
    agent.backend.execute_node = AsyncMock()

    state = {
        "workflow_id": "w1",
        "prd_text": "支付 退款 结算",
        "understanding": "支付系统",
        "sources": [],
    }
    result = identify_subsystems(state, config={"configurable": {"agent": agent}})
    assert len(result["subsystems"]) >= 1
    agent.backend.execute_node.assert_not_called()
