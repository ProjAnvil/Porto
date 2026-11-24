from __future__ import annotations

from unittest.mock import MagicMock

from porto_chatbot.llm.client import LLMClient
from porto_chatbot.llm.types import ToolDef


def _client(mock_chat, max_turns=3, max_tokens=2000, recovery=2):
    c = LLMClient.__new__(LLMClient)
    c._client = mock_chat
    c._native_client = None
    c.logger = MagicMock()
    c.settings = MagicMock(
        agent_max_tool_turns=max_turns,
        agent_request_timeout=10,
        agent_max_tokens=max_tokens,
        max_output_recovery_attempts=recovery,
        agent_retry_attempts=1,  # disable .with_retry so truncation calls reach the mock directly
    )
    return c


def _resp(tool_calls=None, content="", finish_reason="stop"):
    """Stand-in for a langchain AIMessage: tool_calls / content / response_metadata.finish_reason.

    finish_reason defaults to "stop" (a natural end); "length" = hard-cut by max_tokens;
    a falsy value leaves response_metadata absent (mimics old / abnormal responses, which must
    be treated as no length).
    """
    r = MagicMock()
    r.tool_calls = tool_calls or []
    r.content = content
    r.response_metadata = {"finish_reason": finish_reason} if finish_reason else {}
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


# ────────────────── finish_reason="length" handling ──────────────────
# Four layers: detect + escalate + continue + fall back.
# Scenario: in the converging round (no more tool calls) the LLM generates a long report
# outright and gets hard-cut by agent_max_tokens. Mirrors the three layers of Qwen Code's
# "adaptive output token escalation": escalate and resend → continue and concatenate → fall
# back to the truncation flag.


def test_length_escalates_then_converges():
    """First response has finish_reason=length → escalate max_tokens and resend the same convo →
    the second one stops → take the complete text.

    Escalation means "discard this round's truncated output and resend the same context with a
    larger max_tokens" (Qwen layer 1), so the complete second response should simply replace
    the first half-cut text.
    """
    chat = MagicMock()
    chat.bind_tools.return_value = chat
    chat.invoke.side_effect = [
        _resp([], "半截报告", finish_reason="length"),
        _resp([], "完整报告正文", finish_reason="stop"),
    ]
    c = _client(chat, max_turns=4, max_tokens=2000)
    result = c.complete_with_tools("sys", "user", [ToolDef("search", "d", {}, lambda a: "r")])
    assert result.truncated is False
    assert result.text == "完整报告正文"
    assert result.reason is None
    # After escalation max_tokens should be agent_max_tokens*4=8000 and be passed as a kwarg to
    # the second invoke
    assert chat.invoke.call_args_list[1].kwargs.get("max_tokens") == 8000


def test_length_escalate_then_continue_concat():
    """Still length after escalation → inject "please continue + the tail" to continue → the
    pieces concatenate into a converged answer.

    When the escalated resend is still length, the partial output goes in as an assistant
    message followed by "please continue, do not repeat: the last 200 characters" (the Volcano
    Engine approach), and several pieces are concatenated into the complete text.
    """
    chat = MagicMock()
    chat.bind_tools.return_value = chat
    chat.invoke.side_effect = [
        _resp([], "报告第一部分", finish_reason="length"),   # turn 1 triggers escalation
        _resp([], "报告第一部分", finish_reason="length"),   # the escalated resend is still length
        _resp([], "续写第二部分", finish_reason="stop"),      # the continuation converges
    ]
    c = _client(chat, max_turns=4, max_tokens=2000, recovery=2)
    result = c.complete_with_tools("sys", "user", [ToolDef("search", "d", {}, lambda a: "r")])
    assert result.truncated is False
    assert result.text == "报告第一部分续写第二部分"
    assert result.reason is None


def test_length_exhausts_recovery_to_truncated():
    """Still length after escalation + max_rounds of continuation → fallback: truncated=True,
    text='', reason=max_tokens_truncated.

    When the continuation rounds run out without converging, the text is cleared (truncated
    output is never exposed) and truncated is set, so the node layer shows its fixed notice +
    the rerun button.
    """
    chat = MagicMock()
    chat.bind_tools.return_value = chat
    chat.invoke.side_effect = [
        _resp([], "A", finish_reason="length"),  # turn 1: escalate
        _resp([], "A", finish_reason="length"),  # the escalated resend is still length → continue
        _resp([], "B", finish_reason="length"),  # continuation round 1 is still length
        _resp([], "C", finish_reason="length"),  # continuation round 2 is still length → max_rounds used up
    ]
    c = _client(chat, max_turns=4, max_tokens=2000, recovery=2)
    result = c.complete_with_tools("sys", "user", [ToolDef("search", "d", {}, lambda a: "r")])
    assert result.truncated is True
    assert result.text == ""
    assert result.reason == "max_tokens_truncated"


def test_tool_call_length_does_not_execute_partial():
    """tool_calls is non-empty but finish_reason=length (the tool call was cut mid-way) →
    escalate and resend; the broken frame is not executed.

    Per Anthropic: when the truncation point falls inside a tool_use block, that call is
    incomplete and must not be used as is; it has to be resent.
    """
    chat = MagicMock()
    chat.bind_tools.return_value = chat
    executed = []

    def handler(args):
        executed.append(args)
        return "结果"

    chat.invoke.side_effect = [
        _resp([{"name": "search", "args": {"q": "残"}, "id": "1"}], "", finish_reason="length"),
        _resp([{"name": "search", "args": {"q": "完整"}, "id": "2"}], "", finish_reason="stop"),
        _resp([], "最终报告", finish_reason="stop"),
    ]
    c = _client(chat, max_turns=4, max_tokens=2000)
    result = c.complete_with_tools("sys", "user", [ToolDef("search", "d", {}, handler)])
    assert result.truncated is False
    assert result.text == "最终报告"
    # The first, broken tool_call was not executed; only the complete one after escalation was
    assert len(executed) == 1
    assert executed[0] == {"q": "完整"}


def test_anthropic_stop_reason_max_tokens_normalized():
    """ChatAnthropic's stop_reason='max_tokens' is normalized to 'length' and likewise triggers the
    escalated resend.

    Providers name the truncation field differently (OpenAI finish_reason / Anthropic
    stop_reason); after normalization the upstream detection branch works for both, so
    switching provider does not bring the same bug back.
    """
    chat = MagicMock()
    chat.bind_tools.return_value = chat
    r1 = MagicMock()
    r1.tool_calls = []
    r1.content = "半截报告"
    r1.response_metadata = {"stop_reason": "max_tokens"}  # Anthropic style
    chat.invoke.side_effect = [r1, _resp([], "完整报告正文", finish_reason="stop")]
    c = _client(chat, max_turns=4, max_tokens=2000)
    result = c.complete_with_tools("sys", "user", [ToolDef("search", "d", {}, lambda a: "r")])
    assert result.truncated is False
    assert result.text == "完整报告正文"
