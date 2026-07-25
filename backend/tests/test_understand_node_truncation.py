from __future__ import annotations
from unittest.mock import MagicMock
from porto_chatbot.agent.nodes.understand import understand_prd
from porto_chatbot.llm.types import ToolLoopResult
from porto_chatbot.models import AgentStep


def _agent(truncated, turns=4, n_calls=11, max_turns=10):
    agent = MagicMock()
    agent.llm.enabled = True
    agent.llm.complete_with_tools.return_value = ToolLoopResult(
        text="", tool_calls=[object()] * n_calls, turns=turns, truncated=truncated)
    agent.settings.agent_max_tool_turns = max_turns
    # 用真实 _step(只记 data)
    agent._step = lambda name, summary, data: {"steps": [AgentStep(
        name=name, status="completed", summary=summary, data=data)]}
    return agent


def test_understand_truncated_uses_notice_and_meta():
    agent = _agent(truncated=True)
    state = {"workflow_id": "w1", "prd_text": "xxx", "sources": []}
    out = understand_prd(state, config={"configurable": {"agent": agent}})
    assert "未能完成" in out["understanding"]
    tm = out["steps"][0].data["tool_meta"]
    assert tm["truncated"] is True
    assert tm["turns"] == 4
    assert tm["tool_calls"] == 11
    assert tm["max_turns"] == 10


def test_understand_normal_keeps_text():
    agent = MagicMock()
    agent.llm.enabled = True
    agent.llm.complete_with_tools.return_value = ToolLoopResult(
        text="正常理解报告", tool_calls=[object()], turns=2, truncated=False)
    agent.settings.agent_max_tool_turns = 10
    agent._step = lambda name, summary, data: {"steps": [AgentStep(
        name=name, status="completed", summary=summary, data=data)]}
    state = {"workflow_id": "w1", "prd_text": "xxx", "sources": []}
    out = understand_prd(state, config={"configurable": {"agent": agent}})
    assert out["understanding"] == "正常理解报告"
    assert out["steps"][0].data["tool_meta"]["truncated"] is False
