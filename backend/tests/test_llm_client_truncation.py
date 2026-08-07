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
    )
    return c


def _resp(tool_calls=None, content="", finish_reason="stop"):
    """模拟 langchain AIMessage:tool_calls / content / response_metadata.finish_reason。

    finish_reason 默认 "stop"(自然结束);"length" = 被 max_tokens 硬切;
    传 falsy 值则 response_metadata 缺失(模拟旧版/异常响应,应被当成无 length)。
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


# ────────────────── finish_reason="length" 治理(检测 + 升级 + 续写 + 兜底)──────────────────
# 场景:LLM 收敛轮(不再调 tool)直接生成长报告,被 agent_max_tokens 硬切。
# 对齐 Qwen Code「adaptive output token escalation」三层:升级重发 → 续写拼接 → 兜底截断标记。


def test_length_escalates_then_converges():
    """首次 finish_reason=length → 升级 max_tokens 重发同一 convo → 第二次 stop → 取完整 text。

    升级是「丢弃本轮残缺输出、用更大 max_tokens 重发同一上下文」(Qwen 第 1 层),
    因此第二次的完整响应应直接取代第一次的半截文本。
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
    # 升级后 max_tokens 应为 agent_max_tokens*4=8000,并作为 kwarg 传给第二次 invoke
    assert chat.invoke.call_args_list[1].kwargs.get("max_tokens") == 8000


def test_length_escalate_then_continue_concat():
    """升级后仍 length → 注入「请继续 + 尾部」续写 → 拼接收敛。

    升级重发仍 length 时,把部分输出作 assistant 消息 + 「请继续,不要重复:尾部 200 字」
    续写(火山引擎方案),多段拼接成完整文本。
    """
    chat = MagicMock()
    chat.bind_tools.return_value = chat
    chat.invoke.side_effect = [
        _resp([], "报告第一部分", finish_reason="length"),   # turn1 触发升级
        _resp([], "报告第一部分", finish_reason="length"),   # 升级重发仍 length
        _resp([], "续写第二部分", finish_reason="stop"),      # 续写收敛
    ]
    c = _client(chat, max_turns=4, max_tokens=2000, recovery=2)
    result = c.complete_with_tools("sys", "user", [ToolDef("search", "d", {}, lambda a: "r")])
    assert result.truncated is False
    assert result.text == "报告第一部分续写第二部分"
    assert result.reason is None


def test_length_exhausts_recovery_to_truncated():
    """升级 + 续写 max_rounds 后仍 length → 兜底:truncated=True, text='', reason=max_tokens_truncated。

    续写次数用尽仍无法收敛时,清空 text(残缺产出不暴露)、置 truncated,走节点层固定提示 + 重跑按钮。
    """
    chat = MagicMock()
    chat.bind_tools.return_value = chat
    chat.invoke.side_effect = [
        _resp([], "A", finish_reason="length"),  # turn1 升级
        _resp([], "A", finish_reason="length"),  # 升级重发仍 length → 进续写
        _resp([], "B", finish_reason="length"),  # 续写 round1 仍 length
        _resp([], "C", finish_reason="length"),  # 续写 round2 仍 length → max_rounds 用尽
    ]
    c = _client(chat, max_turns=4, max_tokens=2000, recovery=2)
    result = c.complete_with_tools("sys", "user", [ToolDef("search", "d", {}, lambda a: "r")])
    assert result.truncated is True
    assert result.text == ""
    assert result.reason == "max_tokens_truncated"


def test_tool_call_length_does_not_execute_partial():
    """tool_calls 非空但 finish_reason=length(工具调用被中段切)→ 升级重发,不执行残帧。

    Anthropic 官方:截断点落在 tool_use 块时该调用不完整、不可直接使用,必须重发。
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
    # 第一次的残帧 tool_call 未执行,只执行了升级后的完整 tool_call
    assert len(executed) == 1
    assert executed[0] == {"q": "完整"}


def test_anthropic_stop_reason_max_tokens_normalized():
    """ChatAnthropic 的 stop_reason='max_tokens' 归一化为 'length',同样触发升级重发。

    不同 provider 的截断字段名不同(OpenAI finish_reason / Anthropic stop_reason),
    归一化后上层检测分支对两家通用,避免换 provider 复发同一 bug。
    """
    chat = MagicMock()
    chat.bind_tools.return_value = chat
    r1 = MagicMock()
    r1.tool_calls = []
    r1.content = "半截报告"
    r1.response_metadata = {"stop_reason": "max_tokens"}  # Anthropic 风格
    chat.invoke.side_effect = [r1, _resp([], "完整报告正文", finish_reason="stop")]
    c = _client(chat, max_turns=4, max_tokens=2000)
    result = c.complete_with_tools("sys", "user", [ToolDef("search", "d", {}, lambda a: "r")])
    assert result.truncated is False
    assert result.text == "完整报告正文"
