"""TypeSafe 意图引擎：置信度门控、回退与降级（不发真实网络请求）。"""

from __future__ import annotations

from types import SimpleNamespace

import porto_chatbot.intent as intent_mod
from porto_chatbot.models.enums import ChatIntent, IntentEngine, IntentRoutingMode
from porto_chatbot.settings import Settings


class FakeTypesafeClient:
    def __init__(self, choice: str, confidence: float):
        self._resp = SimpleNamespace(
            choices={
                "intent": SimpleNamespace(
                    choice=choice, confidence=confidence, probabilities={},
                ),
            },
        )

    def system_one(self, *, state, questions, **kwargs):
        return self._resp


def _settings(**kw) -> Settings:
    base = dict(chat_typesafe_api_key="k-test", chat_typesafe_confidence_threshold=0.6)
    base.update(kw)
    return Settings(**base)


def test_typesafe_high_confidence_wins(monkeypatch):
    monkeypatch.setattr(
        intent_mod, "_build_typesafe_client",
        lambda s: FakeTypesafeClient("direct", 0.93),
    )
    d = intent_mod.route_chat_intent(
        "在吗", _settings(), llm=None, engine=IntentEngine.TYPESAFE,
    )
    assert d.intent == ChatIntent.DIRECT
    assert d.engine == "typesafe"
    assert d.confidence == 0.93
    assert d.fallback_from is None


def test_typesafe_low_confidence_falls_back_to_builtin(monkeypatch, tmp_path):
    monkeypatch.setattr(
        intent_mod, "_build_typesafe_client",
        lambda s: FakeTypesafeClient("direct", 0.2),
    )
    from porto_chatbot.llm import LLMClient

    s = _settings()
    llm = LLMClient(s)  # 未启用 → builtin 流程落到规则
    d = intent_mod.route_chat_intent(
        "帮我分析支付架构", s, llm, engine=IntentEngine.TYPESAFE,
    )
    assert d.fallback_from == "typesafe"
    assert d.reason.startswith("typesafe:low_confidence")
    assert d.engine in ("builtin_llm", "rule")


def test_typesafe_low_confidence_no_fallback_uses_result(monkeypatch):
    monkeypatch.setattr(
        intent_mod, "_build_typesafe_client",
        lambda s: FakeTypesafeClient("rag", 0.3),
    )
    d = intent_mod.route_chat_intent(
        "查支付文档", _settings(chat_typesafe_low_confidence_fallback=False),
        llm=None, engine=IntentEngine.TYPESAFE,
    )
    assert d.intent == ChatIntent.RAG
    assert d.engine == "typesafe"
    assert d.fallback_from is None


def test_typesafe_missing_key_never_builds_client(monkeypatch):
    def _forbidden(s):
        raise AssertionError("must not build client without api key")

    monkeypatch.setattr(intent_mod, "_build_typesafe_client", _forbidden)
    d = intent_mod.route_chat_intent(
        "你好", _settings(chat_typesafe_api_key=None), llm=None,
        engine=IntentEngine.TYPESAFE,
    )
    assert d.fallback_from == "typesafe"
    assert d.reason.startswith("typesafe:fallback:not_configured")


def test_typesafe_error_degrades_to_builtin(monkeypatch):
    def _boom(s):
        raise RuntimeError("network down")

    monkeypatch.setattr(intent_mod, "_build_typesafe_client", _boom)
    d = intent_mod.route_chat_intent(
        "查支付文档", _settings(), llm=None, engine=IntentEngine.TYPESAFE,
    )
    assert d.fallback_from == "typesafe"
    assert d.reason.startswith("typesafe:fallback:error")


def test_typesafe_unexpected_choice_degrades(monkeypatch):
    monkeypatch.setattr(
        intent_mod, "_build_typesafe_client",
        lambda s: FakeTypesafeClient("chitchat", 0.99),
    )
    d = intent_mod.route_chat_intent(
        "你好", _settings(), llm=None, engine=IntentEngine.TYPESAFE,
    )
    assert d.fallback_from == "typesafe"


def test_typesafe_adaptive_mode_uses_three_labels(monkeypatch):
    seen: dict = {}

    class CapturingClient(FakeTypesafeClient):
        def system_one(self, *, state, questions, **kwargs):
            seen["criteria"] = list(questions["intent"].criteria)
            return self._resp

    monkeypatch.setattr(
        intent_mod, "_build_typesafe_client",
        lambda s: CapturingClient("deep_rag", 0.9),
    )
    d = intent_mod.route_chat_intent(
        "分析支付风控完整架构", _settings(), llm=None,
        routing_mode=IntentRoutingMode.ADAPTIVE, engine=IntentEngine.TYPESAFE,
    )
    assert seen["criteria"] == ["direct", "quick_rag", "deep_rag"]
    assert d.intent == ChatIntent.DEEP_RAG


def test_typesafe_call_uses_seconds_timeout_and_no_retry(monkeypatch):
    """[回归] 调用级 timeout 单位是秒（ms/1000）；重试策略在 client 构造上且 max_retries=0。"""
    import typesafe_sdk as ts_sdk
    from typesafe_sdk import RetryPolicy

    call_kwargs: dict = {}
    ctor_kwargs: dict = {}

    class KwargsClient(FakeTypesafeClient):
        def system_one(self, *, state, questions, **kwargs):
            call_kwargs.update(kwargs)
            return self._resp

    def fake_ctor(**kwargs):
        ctor_kwargs.update(kwargs)
        return KwargsClient("direct", 0.9)

    monkeypatch.setattr(ts_sdk, "TypeSafeClient", fake_ctor)
    intent_mod.route_chat_intent(
        "你好", _settings(chat_typesafe_timeout_ms=2000), llm=None,
        engine=IntentEngine.TYPESAFE,
    )
    assert call_kwargs["timeout"] == 2.0
    assert isinstance(ctor_kwargs["retry"], RetryPolicy)
    assert ctor_kwargs["retry"].max_retries == 0
    assert ctor_kwargs["api_key"] == "k-test"
