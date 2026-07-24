from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from porto_chatbot.llm import LLMClient, ToolDef
from porto_chatbot.settings import Settings


class _StubModel:
    """最小 ChatModel 替身，记录 invoke 入参。"""

    def __init__(self, invoke_returns=None, stream_chunks=None):
        self.invoke_returns = invoke_returns
        self.stream_chunks = stream_chunks or []
        self.invoked_with = None

    def invoke(self, messages, **kw):
        self.invoked_with = messages
        return self.invoke_returns

    def stream(self, messages, **kw):
        for ch in self.stream_chunks:
            yield ch

    def bind_tools(self, tools, **kw):
        return self


def _settings(tmp_path, **over):
    """Settings 构造助手：validation_alias 字段不能通过字段名 kwargs 直接传
    （Settings 未启用 populate_by_name），故走 model_copy(update=...)。
    与 tests/test_llm_modern.py 同模式。"""
    base = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    ).model_copy(
        update=dict(
            agent_api_key="k",
            agent_model="gpt-4.1-mini",
            agent_provider="openai",
        )
    )
    return base.model_copy(update=over)


def test_build_client_disabled_without_key(tmp_path):
    s = Settings(kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "data", log_dir=tmp_path / "logs")
    c = LLMClient(s)
    assert c.enabled is False
    assert c._client is None


def test_build_client_openai(tmp_path):
    c = LLMClient(_settings(tmp_path, agent_provider="openai"))
    assert c.enabled is True
    assert isinstance(c._client, ChatOpenAI)
    assert isinstance(c._client, BaseChatModel)


def test_build_client_anthropic(tmp_path):
    c = LLMClient(_settings(tmp_path, agent_provider="anthropic", agent_model="claude-sonnet-4-5"))
    assert c.enabled is True
    assert isinstance(c._client, ChatAnthropic)


def test_build_client_base_url_passed(tmp_path):
    c = LLMClient(_settings(tmp_path, agent_base_url="https://my.gateway/v1"))
    assert isinstance(c._client, ChatOpenAI)
    assert c._client.openai_api_base == "https://my.gateway/v1"


def test_to_lc_messages_maps_roles(tmp_path):
    c = LLMClient(_settings(tmp_path))
    msgs = c._to_lc_messages(
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
        ]
    )
    assert isinstance(msgs[0], SystemMessage)
    assert isinstance(msgs[1], HumanMessage)
    assert isinstance(msgs[2], AIMessage)


def test_complete_uses_invoke(tmp_path):
    c = LLMClient(_settings(tmp_path))
    c._client = _StubModel(invoke_returns=AIMessage(content="hello"))
    assert c.complete("sys", "u") == "hello"


def test_stream_yields_string_deltas(tmp_path):
    from langchain_core.messages import AIMessageChunk

    c = LLMClient(_settings(tmp_path))
    c._client = _StubModel(stream_chunks=[
        AIMessageChunk(content="he"), AIMessageChunk(content="llo"),
    ])
    assert "".join(c.stream("sys", "u")) == "hello"


def test_structured_parses_and_retries(tmp_path):
    c = LLMClient(_settings(tmp_path))
    responses = iter([AIMessage(content="not json"), AIMessage(content='{"score": 7}')])

    def _invoke(msgs, **kw):
        return next(responses)

    c._client = type("_M", (), {"invoke": staticmethod(_invoke)})()
    parsed = c.complete_structured("sys", "u", {"type": "object"})
    assert parsed == {"score": 7}


def _t(name, handler):
    return ToolDef(
        name=name,
        description="d",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )


def test_with_tools_no_tool_call_returns_text(tmp_path):
    c = LLMClient(_settings(tmp_path))
    bound = type("_B", (), {
        "invoke": lambda self, m, **k: AIMessage(content="final"),
        "bound_tools": [],
    })()
    c._client = type("_M", (), {"bind_tools": lambda self, t: bound})()
    r = c.complete_with_tools("sys", "u", [_t("noop", lambda a: "x")])
    assert r.text == "final"
    assert r.tool_calls == []
    assert r.turns == 1


def test_with_tools_executes_then_finishes(tmp_path):
    seen = []
    script = iter([
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "echo", "args": {"q": "hi"}, "type": "tool_call"}]),
        AIMessage(content="done"),
    ])
    bound = type("_B", (), {"invoke": lambda self, m, **k: next(script)})()
    c = LLMClient(_settings(tmp_path))
    c._client = type("_M", (), {"bind_tools": lambda self, t: bound})()
    r = c.complete_with_tools("sys", "u", [_t("echo", lambda a: seen.append(a) or f"echoed:{a['q']}")])
    assert r.text == "done"
    assert r.turns == 2
    assert r.tool_calls[0].name == "echo"
    assert r.tool_calls[0].arguments == {"q": "hi"}
    assert r.tool_calls[0].result == "echoed:hi"
    assert seen == [{"q": "hi"}]


def test_with_tools_unknown_tool_records_error(tmp_path):
    script = iter([
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "ghost", "args": {}, "type": "tool_call"}]),
        AIMessage(content="recovered"),
    ])
    bound = type("_B", (), {"invoke": lambda self, m, **k: next(script)})()
    c = LLMClient(_settings(tmp_path))
    c._client = type("_M", (), {"bind_tools": lambda self, t: bound})()
    r = c.complete_with_tools("sys", "u", [_t("real", lambda a: "ok")])
    assert r.tool_calls[0].result.startswith("错误：未知工具 ghost")


# ----------------------------- complete_document (方案 B: 原生 SDK) ----- #


class _FakeOpenAICompletions:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _FakeOpenAINative:
    """原生 openai.OpenAI 替身：只暴露 complete_document 用到的 chat.completions。"""

    def __init__(self, response):
        self.chat = SimpleNamespace(completions=_FakeOpenAICompletions(response))


class _FakeAnthropicMessages:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _FakeAnthropicNative:
    """原生 anthropic.Anthropic 替身：只暴露 complete_document 用到的 messages。"""

    def __init__(self, response):
        self.messages = _FakeAnthropicMessages(response)


def test_complete_document_openai_native_sends_file_block(tmp_path):
    """openai 路径:_native_client 收到 file block(file_data 为 data: URL),返回文本。"""
    c = LLMClient(_settings(tmp_path, agent_provider="openai", agent_model="gpt-4.1-mini"))
    # 模拟原生 SDK chat.completions.create 的返回结构
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="# Parsed PRD"))]
    )
    c._native_client = _FakeOpenAINative(fake_resp)

    result = c.complete_document("prd.pdf", b"%PDF-test", "application/pdf", "parse")

    assert result == "# Parsed PRD"
    sent = c._native_client.chat.completions.calls[0]
    assert sent["model"] == "gpt-4.1-mini"
    content = sent["messages"][0]["content"]
    assert content[0]["type"] == "file"
    assert content[0]["file"]["filename"] == "prd.pdf"
    assert content[0]["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert content[1] == {"type": "text", "text": "parse"}


def test_complete_document_anthropic_native_sends_document_block(tmp_path):
    """anthropic 路径:_native_client 收到 document block(base64 source),返回文本。"""
    c = LLMClient(
        _settings(
            tmp_path,
            agent_provider="anthropic",
            agent_model="claude-sonnet-4-5",
        )
    )
    fake_resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="# Parsed")]
    )
    c._native_client = _FakeAnthropicNative(fake_resp)

    result = c.complete_document("prd.pdf", b"%PDF-test", "application/pdf", "parse")

    assert result == "# Parsed"
    sent = c._native_client.messages.calls[0]
    assert sent["model"] == "claude-sonnet-4-5"
    content = sent["messages"][0]["content"]
    assert content[0]["type"] == "document"
    assert content[0]["source"]["type"] == "base64"
    assert content[0]["source"]["media_type"] == "application/pdf"
    assert content[0]["source"]["data"]  # base64 非空
    assert content[1] == {"type": "text", "text": "parse"}


def test_complete_document_returns_none_when_native_client_disabled(tmp_path):
    """_native_client 为 None(缺 api_key)时 complete_document 直接返回 None。"""
    s = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    ).model_copy(
        update={
            "agent_provider": "openai",
            "agent_model": "gpt-4.1-mini",
        }  # 无 agent_api_key
    )
    c = LLMClient(s)
    # enabled 也是 False → document_capabilities.native_pdf 为 False,先短路
    # 强行开启 native_pdf 验证 native_client 短路
    assert c._native_client is None
    assert c.complete_document("prd.pdf", b"%PDF", "application/pdf", "parse") is None
