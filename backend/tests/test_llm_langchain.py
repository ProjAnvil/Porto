from __future__ import annotations

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from porto_chatbot.llm import LLMClient
from porto_chatbot.settings import Settings


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
