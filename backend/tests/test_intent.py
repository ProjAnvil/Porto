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
    # "你好" 规则判 direct，但 LLM 判 rag → 采用 LLM 结果
    llm = _enabled_llm(tmp_path)
    monkeypatch.setattr(llm, "complete_structured", lambda *a, **k: {"intent": "rag", "reason": "需要查库"})
    d = route_chat_intent("你好", None, llm)
    assert d.intent == "rag"
    assert "llm:" in d.reason


def test_intent_invalid_llm_output_falls_back(tmp_path, monkeypatch):
    llm = _enabled_llm(tmp_path)
    monkeypatch.setattr(llm, "complete_structured", lambda *a, **k: None)
    d = route_chat_intent("帮我分析支付风控架构", None, llm)
    assert d.intent == "rag"  # 规则也判 rag（含 支付/风控/架构）
    assert not d.reason.startswith("llm:")  # 用的是规则 reason


def test_intent_empty_message_is_direct():
    d = route_chat_intent("")
    assert d.intent == "direct"
    assert d.reason == "empty_message"


def test_routing_mode_off_skips_routing():
    """off 模式语义上由调用方直接检索，route_chat_intent 仍可调用但行为=返回 RAG。"""
    d = route_chat_intent("你好", routing_mode=IntentRoutingMode.OFF)
    # off 不做 direct 分流，规则不判 greeting
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
    """LLM 不可用时规则降级也应能产出 quick_rag/deep_rag（按复杂度关键词）。"""
    s = Settings(kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "d", log_dir=tmp_path / "l")
    llm = LLMClient(s)
    assert llm.enabled is False
    d = route_chat_intent(
        "分析支付风控架构", None, llm,
        routing_mode=IntentRoutingMode.ADAPTIVE,
    )
    assert d.intent in ("quick_rag", "deep_rag")  # 含 分析/架构 → deep_rag


def test_adaptive_rule_fallback_quick_rag(tmp_path):
    """adaptive 模式下，无 deep 关键词的领域问题应判 quick_rag。"""
    s = Settings(kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "d", log_dir=tmp_path / "l")
    llm = LLMClient(s)
    assert llm.enabled is False
    d = route_chat_intent(
        "支付订单状态查询", None, llm,
        routing_mode=IntentRoutingMode.ADAPTIVE,
    )
    assert d.intent == "quick_rag"


def test_binary_mode_llm_schema_excludes_adaptive_labels(tmp_path, monkeypatch):
    """binary 模式下 LLM schema 只允许 direct/rag，杜绝 quick_rag/deep_rag 渗漏（T1 观察）。"""
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
    """adaptive 模式下 LLM schema 暴露 direct/quick_rag/deep_rag。"""
    captured: dict = {}

    def fake_complete(prompt, user_msg, schema):
        captured["enum"] = schema["properties"]["intent"]["enum"]
        return {"intent": "quick_rag", "reason": "事实查询"}

    llm = _enabled_llm(tmp_path)
    monkeypatch.setattr(llm, "complete_structured", fake_complete)
    d = route_chat_intent("支付订单状态", None, llm, routing_mode=IntentRoutingMode.ADAPTIVE)
    assert captured["enum"] == ["direct", "quick_rag", "deep_rag"]
    assert d.intent == "quick_rag"
