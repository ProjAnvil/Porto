from __future__ import annotations

from porto_chatbot.intent import route_chat_intent
from porto_chatbot.llm import LLMClient
from porto_chatbot.settings import Settings


def _enabled_llm(tmp_path) -> LLMClient:
    s = Settings(
        kb_path=tmp_path / "kb",
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
    s = Settings(kb_path=tmp_path / "kb", data_dir=tmp_path / "d", log_dir=tmp_path / "l")
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
