"""Tests for the LLM governance layer: InMemoryRateLimiter rate limiting + .with_retry
exponential backoff.

Both use native langchain-core primitives. Attachment points:
- Rate limiting: ``rate_limiter`` is passed when the chat model is built
  (``agent_rate_limit_rps``; None disables it)
- Retry: the call sites are wrapped with ``_with_retry()`` (``agent_retry_attempts`` total
  attempts; 1 disables it)
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_openai import ChatOpenAI

from porto_chatbot.llm import LLMClient, ToolDef
from porto_chatbot.settings import Settings


def _settings(tmp_path, **over):
    """Same pattern as tests/test_llm_langchain.py: alias fields go through
    model_copy(update=...)."""
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


# ----------------------------- Settings defaults ----------------------------- #


def test_governance_settings_defaults(tmp_path):
    s = _settings(tmp_path)
    assert s.agent_retry_attempts == 3
    assert s.agent_rate_limit_rps == 2.0


# ------------------------------- Rate limiting ------------------------------- #


def test_rate_limiter_attached_by_default(tmp_path):
    c = LLMClient(_settings(tmp_path))
    assert isinstance(c._client.rate_limiter, InMemoryRateLimiter)
    assert c._client.rate_limiter.requests_per_second == 2.0


def test_rate_limiter_disabled_when_rps_none(tmp_path):
    c = LLMClient(_settings(tmp_path, agent_rate_limit_rps=None))
    assert c._client.rate_limiter is None


# ---------------------------------- Retry ---------------------------------- #


def test_retry_recovers_transient_error(monkeypatch, tmp_path):
    """A transient error (e.g. 429) → .with_retry retries successfully, 2 underlying calls in
    total."""
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
    """agent_retry_attempts=1 → no retry; the first failure is raised directly."""
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
    """LLM calls inside the tool loop go through the retry wrapper as well (with_retry is attached
    after bind_tools)."""
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
