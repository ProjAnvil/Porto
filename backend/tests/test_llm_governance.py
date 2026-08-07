"""LLM 治理层测试：InMemoryRateLimiter 限流 + .with_retry 指数退避重试。

均使用 langchain-core 原生原语，挂接点：
- 限流：构造 chat model 时传 ``rate_limiter``（``agent_rate_limit_rps``，None 关闭）
- 重试：调用点 ``_with_retry()`` 包装（``agent_retry_attempts`` 总尝试次数，1 关闭）
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_openai import ChatOpenAI

from porto_chatbot.llm import LLMClient, ToolDef
from porto_chatbot.settings import Settings


def _settings(tmp_path, **over):
    """与 tests/test_llm_langchain.py 同模式：alias 字段走 model_copy(update=...)。"""
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


# ------------------------------- settings 默认值 ------------------------- #


def test_governance_settings_defaults(tmp_path):
    s = _settings(tmp_path)
    assert s.agent_retry_attempts == 3
    assert s.agent_rate_limit_rps == 2.0


# ------------------------------- 限流 ------------------------------------ #


def test_rate_limiter_attached_by_default(tmp_path):
    c = LLMClient(_settings(tmp_path))
    assert isinstance(c._client.rate_limiter, InMemoryRateLimiter)
    assert c._client.rate_limiter.requests_per_second == 2.0


def test_rate_limiter_disabled_when_rps_none(tmp_path):
    c = LLMClient(_settings(tmp_path, agent_rate_limit_rps=None))
    assert c._client.rate_limiter is None


# ------------------------------- 重试 ------------------------------------ #


def test_retry_recovers_transient_error(monkeypatch, tmp_path):
    """瞬时错误（如 429）→ .with_retry 重试成功，底层共调用 2 次。"""
    calls = {"n": 0}

    def flaky_invoke(self, input, config=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 rate limit")
        return AIMessage(content="ok")

    monkeypatch.setattr(ChatOpenAI, "invoke", flaky_invoke)
    c = LLMClient(_settings(tmp_path))
    assert c.complete("sys", "u") == "ok"
    assert calls["n"] == 2


def test_retry_attempts_one_disables_retry(monkeypatch, tmp_path):
    """agent_retry_attempts=1 → 不重试，首次失败直接上抛。"""
    calls = {"n": 0}

    def always_fail(self, input, config=None, **kwargs):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(ChatOpenAI, "invoke", always_fail)
    c = LLMClient(_settings(tmp_path, agent_retry_attempts=1))
    with pytest.raises(RuntimeError):
        c.complete("sys", "u")
    assert calls["n"] == 1


def test_retry_applies_in_tool_loop(monkeypatch, tmp_path):
    """工具循环内的 LLM 调用同样走重试包装（bind_tools 之后再挂 with_retry）。"""
    calls = {"n": 0}

    def flaky_invoke(self, input, config=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("503 overloaded")
        return AIMessage(content="final")

    monkeypatch.setattr(ChatOpenAI, "invoke", flaky_invoke)
    c = LLMClient(_settings(tmp_path))
    tool = ToolDef(
        name="noop",
        description="d",
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=lambda a: "x",
    )
    r = c.complete_with_tools("sys", "u", [tool])
    assert r.text == "final"
    assert calls["n"] == 2
