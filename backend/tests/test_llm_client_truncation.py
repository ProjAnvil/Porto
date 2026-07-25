from __future__ import annotations
from unittest.mock import MagicMock
from porto_chatbot.llm.client import LLMClient
from porto_chatbot.llm.types import ToolDef


def _client(mock_chat, max_turns=3):
    c = LLMClient.__new__(LLMClient)
    c._client = mock_chat
    c._native_client = None
    c.logger = MagicMock()
    c.settings = MagicMock(agent_max_tool_turns=max_turns, agent_request_timeout=10)
    return c


def _resp(tool_calls=None, content=""):
    r = MagicMock()
    r.tool_calls = tool_calls or []
    r.content = content
    return r


def test_truncation_clears_text_and_marks_truncated():
    chat = MagicMock()
    chat.bind_tools.return_value = chat
    chat.invoke.side_effect = [
        _resp([{"name": "search", "args": {}, "id": "1"}], "我再查一下1"),
        _resp([{"name": "search", "args": {}, "id": "2"}], "我再查一下2"),
        _resp([{"name": "search", "args": {}, "id": "3"}], "我再查一下3"),
    ]
    c = _client(chat, max_turns=3)
    result = c.complete_with_tools("sys", "user", [ToolDef("search", "d", {}, lambda a: "结果")])
    assert result.truncated is True
    assert result.text == ""
    assert result.turns == 3
    assert len(result.tool_calls) == 3


def test_normal_convergence_returns_final_text():
    chat = MagicMock()
    chat.bind_tools.return_value = chat
    chat.invoke.side_effect = [
        _resp([{"name": "search", "args": {}, "id": "1"}], "查一下"),
        _resp([], "最终报告正文"),
    ]
    c = _client(chat, max_turns=4)
    result = c.complete_with_tools("sys", "user", [ToolDef("search", "d", {}, lambda a: "结果")])
    assert result.truncated is False
    assert result.text == "最终报告正文"
    assert result.turns == 2
    assert len(result.tool_calls) == 1
