from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from porto_chatbot.llm import LLMClient, ToolDef, _try_parse_json
from porto_chatbot.settings import Settings

# ----------------------------- fake ChatModel ---------------------------- #


class ScriptedModel:
    """按脚本返回 invoke/stream 结果的最小 ChatModel 替身。

    - invoke 序列：每轮弹一个 AIMessage（可带 tool_calls）
    - stream 序列：弹 AIMessageChunk 列表
    - bind_tools 返回自身（LLMClient 在 bound 上 invoke）
    """

    def __init__(self, invoke_script=None, stream_script=None):
        self._invoke = list(invoke_script or [])
        self._stream = list(stream_script or [])
        self.invoke_calls: list = []
        self.bound_tools = None

    def invoke(self, messages, **kw):
        self.invoke_calls.append(messages)
        if not self._invoke:
            raise AssertionError("ScriptedModel invoke script exhausted")
        return self._invoke.pop(0)

    def stream(self, messages, **kw):
        text = self._stream[0].content if self._stream else ""
        for i in range(0, len(text), 3):
            yield AIMessageChunk(content=text[i : i + 3])

    def bind_tools(self, tools, **kw):
        self.bound_tools = tools
        return self


# ----------------------------- fixtures ----------------------------- #


@pytest.fixture()
def enabled_settings(tmp_path):
    return Settings(
        kb_dirs=[tmp_path / "kb"],
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


def _wire(client: LLMClient, invoke_script, stream_script=None) -> ScriptedModel:
    m = ScriptedModel(invoke_script=invoke_script, stream_script=stream_script)
    client._client = m
    return m


# ----------------------------- 降级路径 ----------------------------- #


def test_disabled_client_returns_none(tmp_path):
    s = Settings(
        kb_dirs=[tmp_path / "kb"],
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
    _wire(client, [AIMessage(content="hello")])
    assert client.complete("sys", "u") == "hello"


def test_complete_accepts_messages(client):
    _wire(client, [AIMessage(content="ok")])
    result = client.complete(
        "ignored-system",
        "ignored-user",
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ],
    )
    assert result == "ok"
    sent = client._client.invoke_calls[0]
    # 消息列表已由 _to_lc_messages 转为 BaseMessage；最后一条应为 HumanMessage("u2")
    assert isinstance(sent[-1], HumanMessage)
    assert sent[-1].content == "u2"
    assert len(sent) == 4


def test_document_capabilities_require_enabled_supported_model(client):
    _wire(client, [])
    client.settings.agent_model = "m"
    assert client.document_capabilities.native_pdf is False  # fixture model "m" is unknown
    client.settings.agent_model = "gpt-4.1-mini"
    assert client.document_capabilities.native_pdf is True

    client._client = None
    assert client.document_capabilities.native_pdf is False


# ----------------------------- complete_structured ----------------------------- #


def test_structured_parses_clean_json(client):
    _wire(client, [AIMessage(content='{"score": 10, "verdict": "PASS"}')])
    parsed = client.complete_structured("sys", "u", {"type": "object"})
    assert parsed == {"score": 10, "verdict": "PASS"}


def test_structured_parses_fenced_json(client):
    _wire(client, [AIMessage(content='```json\n{"score": 8}\n```')])
    assert client.complete_structured("sys", "u", {"type": "object"}) == {"score": 8}


def test_structured_retries_then_succeeds(client):
    _wire(
        client,
        [
            AIMessage(content="I think the answer is..."),  # 首次坏
            AIMessage(content='{"score": 9}'),  # 重试好
        ],
    )
    assert client.complete_structured("sys", "u", {"type": "object"}) == {"score": 9}
    assert len(client._client.invoke_calls) == 2


def test_structured_returns_none_after_failed_retry(client):
    _wire(
        client,
        [
            AIMessage(content="nope"),
            AIMessage(content="still nope"),
        ],
    )
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
    _wire(client, [AIMessage(content="final answer")])
    r = client.complete_with_tools("sys", "u", [_tool("noop", lambda a: "x")])
    assert r.text == "final answer"
    assert r.tool_calls == []
    assert r.turns == 1
    assert r.truncated is False


def test_tool_loop_executes_then_finishes(client):
    seen: list[dict] = []
    _wire(
        client,
        [
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "echo", "args": {"q": "hi"}, "type": "tool_call"}],
            ),
            AIMessage(content="done"),
        ],
    )

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
    _wire(
        client,
        [
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "ghost", "args": {}, "type": "tool_call"}],
            ),
            AIMessage(content="recovered"),
        ],
    )
    r = client.complete_with_tools("sys", "u", [_tool("real", lambda a: "ok")])
    assert r.text == "recovered"
    assert r.tool_calls[0].result.startswith("错误：未知工具 ghost")


def test_tool_loop_handler_exception_does_not_crash(client):
    _wire(
        client,
        [
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "boom", "args": {}, "type": "tool_call"}],
            ),
            AIMessage(content="after-boom"),
        ],
    )

    def boom(args):
        raise RuntimeError("boom")

    r = client.complete_with_tools("sys", "u", [_tool("boom", boom)])
    assert r.text == "after-boom"
    assert "执行失败" in r.tool_calls[0].result


def test_tool_loop_truncated_at_max_turns(client, enabled_settings):
    """LLM 一直调 tool 不收尾，到 max_turns 必须截断。"""
    # 每轮都调 tool；max_turns=2 → 2 轮 tool，然后截断:清空 text(B 方案,不做收尾 invoke)
    _wire(
        client,
        [
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "loop", "args": {}, "type": "tool_call"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"id": "c2", "name": "loop", "args": {}, "type": "tool_call"}],
            ),
        ],
    )
    r = client.complete_with_tools("sys", "u", [_tool("loop", lambda a: "again")], max_turns=2)
    assert r.truncated is True
    assert r.turns == 2
    assert len(r.tool_calls) == 2
    assert r.text == ""


def test_tool_loop_no_tools_falls_back_to_complete(client):
    _wire(client, [AIMessage(content="plain")])
    r = client.complete_with_tools("sys", "u", [])
    assert r.text == "plain"
    assert r.tool_calls == []


def test_tool_loop_carries_history_across_turns(client):
    """第二轮 LLM 应能看到第一轮 tool 结果（ToolMessage 进 convo）。"""
    _wire(
        client,
        [
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "echo", "args": {"q": "x"}, "type": "tool_call"}],
            ),
            AIMessage(content="final"),
        ],
    )
    client.complete_with_tools("sys", "u", [_tool("echo", lambda a: "ECHO")])
    # 第二次 invoke 的 messages 应包含 ToolMessage（tool 结果回填进 convo）
    second_msgs = client._client.invoke_calls[1]
    assert any(isinstance(m, ToolMessage) for m in second_msgs)


# ----------------------------- stream ----------------------------- #


def test_stream_yields_deltas(client):
    _wire(client, [], stream_script=[AIMessage(content="hello world")])
    chunks = list(client.stream("sys", "u"))
    assert "".join(chunks) == "hello world"
    assert len(chunks) >= 2  # 确实分片
