from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from porto_chatbot.llm import LLMClient, ToolDef, _try_parse_json
from porto_chatbot.settings import Settings

# ----------------------------- fake openai client ---------------------------- #


class FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id: str, name: str, args: dict[str, Any]):
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, json.dumps(args))


class FakeMessage:
    def __init__(self, content: str | None = None, tool_calls: list | None = None):
        self.content = content
        self.tool_calls = tool_calls


class FakeResponse:
    """openai 风格的非流式响应。"""

    def __init__(self, message: FakeMessage):
        self.choices = [SimpleNamespace(message=message)]


class FakeStreamChunk:
    def __init__(self, content: str | None):
        self.choices = [SimpleNamespace(delta=SimpleNamespace(content=content))]


class FakeCompletions:
    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("FakeCompletions script exhausted")
        item = self._script.pop(0)
        if kwargs.get("stream"):
            return self._stream(item)
        return item

    def _stream(self, item):
        text = item.choices[0].message.content or ""
        for i in range(0, len(text), 3):
            yield FakeStreamChunk(text[i : i + 3])


class FakeChat:
    def __init__(self, script: list):
        self.completions = FakeCompletions(script)


class FakeOpenAI:
    def __init__(self, script: list):
        self.chat = FakeChat(script)


# ----------------------------- fixtures ----------------------------- #


@pytest.fixture()
def enabled_settings(tmp_path):
    return Settings(
        kb_path=tmp_path / "kb",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        agent_api_key="k",
        agent_provider="openai",
        agent_model="m",
    )


@pytest.fixture()
def client(enabled_settings):
    c = LLMClient(enabled_settings)
    return c


def _wire(client: LLMClient, script: list):
    client._client = FakeOpenAI(script)


# ----------------------------- 降级路径 ----------------------------- #


def test_disabled_client_returns_none(tmp_path):
    s = Settings(
        kb_path=tmp_path / "kb",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )  # 无 api_key
    c = LLMClient(s)
    assert c.enabled is False
    assert c.complete("sys", "u") is None
    assert c.complete_structured("sys", "u", {"type": "object"}) is None
    assert c.complete_with_tools("sys", "u", []).text == ""
    assert list(c.stream("sys", "u")) == []


# ----------------------------- _try_parse_json ----------------------------- #


def test_try_parse_json_plain():
    assert _try_parse_json('{"a": 1}') == {"a": 1}


def test_try_parse_json_fenced():
    assert _try_parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_try_parse_json_embedded():
    assert _try_parse_json('结果如下：{"a": 1} 完毕') == {"a": 1}


def test_try_parse_json_invalid():
    assert _try_parse_json("not json") is None
    assert _try_parse_json("") is None


def test_try_parse_json_array_returns_none():
    """约定：只接受对象，数组不算结构化对象。"""
    assert _try_parse_json("[1, 2, 3]") is None


# ----------------------------- complete / multi-turn ----------------------------- #


def test_complete_legacy_system_user(client):
    _wire(client, [FakeResponse(FakeMessage(content="hello"))])
    assert client.complete("sys", "u") == "hello"


def test_complete_accepts_messages(client):
    _wire(client, [FakeResponse(FakeMessage(content="ok"))])
    result = client.complete("ignored-system", "ignored-user", messages=[
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ])
    assert result == "ok"
    sent = client._client.chat.completions.calls[0]["messages"]
    assert sent[-1] == {"role": "user", "content": "u2"}
    assert len(sent) == 4


# ----------------------------- complete_structured ----------------------------- #


def test_structured_parses_clean_json(client):
    _wire(client, [FakeResponse(FakeMessage(content='{"score": 10, "verdict": "PASS"}'))])
    parsed = client.complete_structured("sys", "u", {"type": "object"})
    assert parsed == {"score": 10, "verdict": "PASS"}


def test_structured_parses_fenced_json(client):
    _wire(client, [FakeResponse(FakeMessage(content='```json\n{"score": 8}\n```'))])
    assert client.complete_structured("sys", "u", {"type": "object"}) == {"score": 8}


def test_structured_retries_then_succeeds(client):
    _wire(client, [
        FakeResponse(FakeMessage(content="I think the answer is...")),  # 首次坏
        FakeResponse(FakeMessage(content='{"score": 9}')),             # 重试好
    ])
    assert client.complete_structured("sys", "u", {"type": "object"}) == {"score": 9}
    assert len(client._client.chat.completions.calls) == 2


def test_structured_returns_none_after_failed_retry(client):
    _wire(client, [
        FakeResponse(FakeMessage(content="nope")),
        FakeResponse(FakeMessage(content="still nope")),
    ])
    assert client.complete_structured("sys", "u", {"type": "object"}) is None


# ----------------------------- complete_with_tools ----------------------------- #


def _tool(name: str, handler) -> ToolDef:
    return ToolDef(
        name=name,
        description="d",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )


def test_tool_loop_no_tool_call_returns_text(client):
    _wire(client, [FakeResponse(FakeMessage(content="final answer"))])
    r = client.complete_with_tools("sys", "u", [_tool("noop", lambda a: "x")])
    assert r.text == "final answer"
    assert r.tool_calls == []
    assert r.turns == 1
    assert r.truncated is False


def test_tool_loop_executes_then_finishes(client):
    seen: list[dict] = []
    _wire(client, [
        FakeResponse(FakeMessage(content="", tool_calls=[FakeToolCall("c1", "echo", {"q": "hi"})])),
        FakeResponse(FakeMessage(content="done")),
    ])

    def echo(args):
        seen.append(args)
        return f"echoed:{args['q']}"

    r = client.complete_with_tools("sys", "u", [_tool("echo", echo)])
    assert r.text == "done"
    assert r.turns == 2
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0].name == "echo"
    assert r.tool_calls[0].arguments == {"q": "hi"}
    assert r.tool_calls[0].result == "echoed:hi"
    assert seen == [{"q": "hi"}]


def test_tool_loop_unknown_tool_records_error(client):
    _wire(client, [
        FakeResponse(FakeMessage(content="", tool_calls=[FakeToolCall("c1", "ghost", {})])),
        FakeResponse(FakeMessage(content="recovered")),
    ])
    r = client.complete_with_tools("sys", "u", [_tool("real", lambda a: "ok")])
    assert r.text == "recovered"
    assert r.tool_calls[0].result.startswith("错误：未知工具 ghost")


def test_tool_loop_handler_exception_does_not_crash(client):
    _wire(client, [
        FakeResponse(FakeMessage(content="", tool_calls=[FakeToolCall("c1", "boom", {})])),
        FakeResponse(FakeMessage(content="after-boom")),
    ])

    def boom(args):
        raise RuntimeError("boom")

    r = client.complete_with_tools("sys", "u", [_tool("boom", boom)])
    assert r.text == "after-boom"
    assert "执行失败" in r.tool_calls[0].result


def test_tool_loop_truncated_at_max_turns(client, enabled_settings):
    """LLM 一直调 tool 不收尾，到 max_turns 必须截断。"""
    # 每轮都调 tool；max_turns=2 → 2 轮 tool，然后 1 次 complete 收尾
    script = [
        FakeResponse(FakeMessage(content="", tool_calls=[FakeToolCall("c1", "loop", {})])),
        FakeResponse(FakeMessage(content="", tool_calls=[FakeToolCall("c2", "loop", {})])),
        FakeResponse(FakeMessage(content="final")),
    ]
    _wire(client, script)
    r = client.complete_with_tools("sys", "u", [_tool("loop", lambda a: "again")], max_turns=2)
    assert r.truncated is True
    assert r.turns == 2
    assert len(r.tool_calls) == 2
    assert r.text == "final"


def test_tool_loop_no_tools_falls_back_to_complete(client):
    _wire(client, [FakeResponse(FakeMessage(content="plain"))])
    r = client.complete_with_tools("sys", "u", [])
    assert r.text == "plain"
    assert r.tool_calls == []


def test_tool_loop_carries_history_across_turns(client):
    """第二轮 LLM 应能看到第一轮 tool 结果（对话历史正确传递）。"""
    captured: list[list] = []

    class CapturingCompletions(FakeCompletions):
        def create(self, **kwargs):
            if not kwargs.get("stream"):
                captured.append(kwargs.get("messages"))
            return super().create(**kwargs)

    script = [
        FakeResponse(FakeMessage(content="", tool_calls=[FakeToolCall("c1", "echo", {"q": "x"})])),
        FakeResponse(FakeMessage(content="final")),
    ]
    client._client = FakeOpenAI(script)
    client._client.chat.completions = CapturingCompletions(script)
    client.complete_with_tools("sys", "u", [_tool("echo", lambda a: "ECHO")])
    # 第二次调用的 messages 应包含 tool 结果消息
    second_msgs = captured[1]
    roles = [m["role"] for m in second_msgs]
    assert "tool" in roles


# ----------------------------- stream ----------------------------- #


def test_stream_yields_deltas(client):
    _wire(client, [FakeResponse(FakeMessage(content="hello world"))])
    chunks = list(client.stream("sys", "u"))
    assert "".join(chunks) == "hello world"
    assert len(chunks) >= 2  # 确实分片
