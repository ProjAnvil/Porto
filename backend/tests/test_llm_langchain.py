from __future__ import annotations

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from porto_chatbot.llm import LLMClient
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
