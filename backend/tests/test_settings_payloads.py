"""Contract tests for the intent-engine and typesafe config keys (rag_chat namespace)."""

from porto_chatbot.config_store import RAG_CHAT_SETTING_KEYS, SENSITIVE_SETTING_KEYS
from porto_chatbot.models import RagChatSettingsPayload
from porto_chatbot.models.enums import IntentEngine
from porto_chatbot.settings import Settings


def test_intent_engine_defaults_to_builtin_llm():
    s = Settings()
    assert s.chat_intent_engine == IntentEngine.BUILTIN_LLM
    assert s.chat_typesafe_timeout_ms == 2000
    assert s.chat_typesafe_confidence_threshold == 0.6
    assert s.chat_typesafe_low_confidence_fallback is True
    assert s.chat_typesafe_api_key is None
    assert s.chat_typesafe_base_url is None


def test_rag_chat_keys_cover_typesafe_settings_and_condense():
    assert {
        "intent_engine",
        "chat_typesafe_api_key",
        "chat_typesafe_base_url",
        "chat_typesafe_timeout_ms",
        "chat_typesafe_confidence_threshold",
        "chat_typesafe_low_confidence_fallback",
        # Fix for existing data: condense_enabled was missing from the key set, so it could not be
        # read back after saving
        "condense_enabled",
    } <= RAG_CHAT_SETTING_KEYS
    assert "chat_typesafe_api_key" in SENSITIVE_SETTING_KEYS


def test_rag_chat_payload_partial_update_keeps_other_fields_none():
    p = RagChatSettingsPayload(intent_engine="typesafe")
    assert p.chat_typesafe_api_key is None
    assert p.condense_enabled is None
