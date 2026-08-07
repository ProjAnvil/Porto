from __future__ import annotations

from porto_chatbot.models import MessageRecord, SessionFact
from porto_chatbot.settings import Settings


def test_session_fact_defaults():
    fact = SessionFact(
        id="f1",
        session_id="s1",
        category="user_decision",
        content="登录采用 OAuth",
        source_msg_id="m1",
        created_at="2026-07-27T00:00:00Z",
        updated_at="2026-07-27T00:00:00Z",
    )
    assert fact.status == "active"
    assert fact.category == "user_decision"


def test_settings_facts_defaults():
    s = Settings()
    assert s.facts_enabled is True
    assert s.facts_max_per_category == 20
    assert s.facts_similarity_threshold == 0.5
    assert s.facts_recent_context_turns == 6
    assert s.facts_provider is None
    assert s.facts_model is None


def test_message_record_has_intent_and_indexed():
    r = MessageRecord(
        id="m1", session_id="s1", role="user", content="hello",
        created_at="2026-01-01T00:00:00Z",
    )
    assert r.intent is None
    assert r.indexed is False
    assert r.metadata == {}


def test_message_record_with_intent():
    r = MessageRecord(
        id="m2", session_id="s1", role="assistant", content="answer",
        intent="rag", indexed=True, created_at="2026-01-01T00:00:00Z",
    )
    assert r.intent == "rag"
    assert r.indexed is True
