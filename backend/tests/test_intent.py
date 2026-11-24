from __future__ import annotations

from porto_chatbot.intent import route_chat_intent
from porto_chatbot.llm import LLMClient
from porto_chatbot.models.enums import ChatIntent, IntentRoutingMode
from porto_chatbot.settings import Settings


def _enabled_llm(tmp_path) -> LLMClient:
    s = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "d",
        log_dir=tmp_path / "l",
        agent_provider="openai",
        agent_model="m",
    )
    s.agent_api_key = "k"
    return LLMClient(s)


def test_intent_no_llm_uses_rules():
    d = route_chat_intent("thanks")
    assert d.intent == "direct"
    assert d.reason == "smalltalk_or_help"


def test_intent_disabled_llm_falls_back_to_rules(tmp_path):
    s = Settings(kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "d", log_dir=tmp_path / "l")
    llm = LLMClient(s)
    assert llm.enabled is False
    d = route_chat_intent("你好", None, llm)
    assert d.intent == "direct"
    assert d.reason == "greeting"


def test_intent_llm_overrides_rules(tmp_path, monkeypatch):
    # "你好" is classified as direct by the rule, but the LLM says rag → the LLM result wins
    llm = _enabled_llm(tmp_path)
    monkeypatch.setattr(llm, "complete_structured", lambda *a, **k: {"intent": "rag", "reason": "需要查库"})
    d = route_chat_intent("你好", None, llm)
    assert d.intent == "rag"
    assert "llm:" in d.reason


def test_intent_invalid_llm_output_falls_back(tmp_path, monkeypatch):
    llm = _enabled_llm(tmp_path)
    monkeypatch.setattr(llm, "complete_structured", lambda *a, **k: None)
    d = route_chat_intent("帮我分析支付风控架构", None, llm)
    assert d.intent == "rag"  # the rule also says rag (contains 支付 / 风控 / 架构)
    assert not d.reason.startswith("llm:")  # the rule reason is used


def test_intent_empty_message_is_direct():
    d = route_chat_intent("")
    assert d.intent == "direct"
    assert d.reason == "empty_message"


def test_routing_mode_off_skips_routing():
    """In off mode the caller is supposed to retrieve directly; route_chat_intent can still be
    called but then behaves as "return RAG"."""
    d = route_chat_intent("你好", routing_mode=IntentRoutingMode.OFF)
    # off does no direct routing; the rule does not classify greetings
    assert d.intent in (ChatIntent.RAG, ChatIntent.QUICK_RAG, ChatIntent.DEEP_RAG)


def test_adaptive_llm_classifies_three_way(tmp_path, monkeypatch):
    llm = _enabled_llm(tmp_path)
    monkeypatch.setattr(
        llm, "complete_structured",
        lambda *a, **k: {"intent": "deep_rag", "reason": "复杂架构问题"},
    )
    d = route_chat_intent(
        "分析支付风控的完整架构", None, llm,
        routing_mode=IntentRoutingMode.ADAPTIVE,
    )
    assert d.intent == "deep_rag"


def test_adaptive_rule_fallback(tmp_path):
    """When the LLM is unavailable, the rule fallback should still produce quick_rag / deep_rag
    (by complexity keywords)."""
    s = Settings(kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "d", log_dir=tmp_path / "l")
    llm = LLMClient(s)
    assert llm.enabled is False
    d = route_chat_intent(
        "分析支付风控架构", None, llm,
        routing_mode=IntentRoutingMode.ADAPTIVE,
    )
    assert d.intent in ("quick_rag", "deep_rag")  # contains 分析 / 架构 → deep_rag


def test_adaptive_rule_fallback_quick_rag(tmp_path):
    """In adaptive mode, a domain question without deep keywords should be classified as
    quick_rag."""
    s = Settings(kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "d", log_dir=tmp_path / "l")
    llm = LLMClient(s)
    assert llm.enabled is False
    d = route_chat_intent(
        "支付订单状态查询", None, llm,
        routing_mode=IntentRoutingMode.ADAPTIVE,
    )
    assert d.intent == "quick_rag"


def test_binary_mode_llm_schema_excludes_adaptive_labels(tmp_path, monkeypatch):
    """In binary mode the LLM schema only allows direct / rag, ruling out quick_rag / deep_rag
    leaking in (observed in T1)."""
    captured: dict = {}

    def fake_complete(prompt, user_msg, schema):
        captured["enum"] = schema["properties"]["intent"]["enum"]
        return {"intent": "rag", "reason": "binary"}

    llm = _enabled_llm(tmp_path)
    monkeypatch.setattr(llm, "complete_structured", fake_complete)
    d = route_chat_intent("分析支付架构", None, llm, routing_mode=IntentRoutingMode.BINARY)
    assert captured["enum"] == ["direct", "rag"]
    assert d.intent == "rag"


def test_adaptive_mode_llm_schema_includes_three_labels(tmp_path, monkeypatch):
    """In adaptive mode the LLM schema exposes direct / quick_rag / deep_rag."""
    captured: dict = {}

    def fake_complete(prompt, user_msg, schema):
        captured["enum"] = schema["properties"]["intent"]["enum"]
        return {"intent": "quick_rag", "reason": "事实查询"}

    llm = _enabled_llm(tmp_path)
    monkeypatch.setattr(llm, "complete_structured", fake_complete)
    d = route_chat_intent("支付订单状态", None, llm, routing_mode=IntentRoutingMode.ADAPTIVE)
    assert captured["enum"] == ["direct", "quick_rag", "deep_rag"]
    assert d.intent == "quick_rag"


# ── Intent engines (builtin_llm / rule / typesafe) ──


def test_rule_engine_skips_llm_entirely(tmp_path):
    from porto_chatbot.models.enums import IntentEngine

    s = Settings(kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "d", log_dir=tmp_path / "l")
    llm = _enabled_llm(tmp_path)
    called = {"n": 0}
    monkey = llm.complete_structured  # noqa: F841 — make sure the reference exists
    llm.complete_structured = lambda *a, **k: called.update(n=called["n"] + 1)
    d = route_chat_intent("你好", s, llm, engine=IntentEngine.RULE)
    assert d.intent == ChatIntent.DIRECT
    assert d.engine == "rule"
    assert d.confidence is None
    assert called["n"] == 0  # the rule engine never touches the LLM


def test_default_engine_is_builtin_llm(tmp_path):
    s = Settings(kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "d", log_dir=tmp_path / "l")
    llm = LLMClient(s)  # not enabled → the builtin flow falls back to the rule internally
    d = route_chat_intent("帮我查知识库里的支付文档", s, llm)
    assert d.engine == "rule"  # llm.enabled=False → the rule wins


def test_off_mode_reports_empty_engine():
    from porto_chatbot.models.enums import IntentEngine

    d = route_chat_intent(
        "你好", routing_mode=IntentRoutingMode.OFF, engine=IntentEngine.RULE,
    )
    assert d.intent == ChatIntent.RAG
    assert d.reason == "routing_off"
    assert d.engine == ""
