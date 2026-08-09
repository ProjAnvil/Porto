"""Tests for profile-aware vector delivery tracking."""
from __future__ import annotations

from porto_chatbot.memory.conversation_memory import ConversationMemory
from porto_chatbot.memory.persist import persist_turn
from porto_chatbot.memory.session_store import SessionStore
from porto_chatbot.memory.vector_delivery import VectorDeliveryStore


def _add_msg(sessions, **kw):
    return sessions.add_message(
        session_id=kw.get("session_id", "s1"),
        role=kw.get("role", "user"),
        content=kw.get("content", "hello"),
        intent=kw.get("intent", "rag"),
        create_vector_intent=kw.get("create_vector_intent", False),
    )


# ── intent creation ──


def test_index_vector_true_persists_intent(sample_settings):
    sessions = SessionStore(sample_settings)
    delivery = VectorDeliveryStore(sample_settings)

    msg = _add_msg(sessions, content="indexed msg", create_vector_intent=True)
    assert delivery.intents_exist(msg.id)


def test_index_vector_false_creates_no_intent(sample_settings):
    sessions = SessionStore(sample_settings)
    delivery = VectorDeliveryStore(sample_settings)

    msg = _add_msg(sessions, content="chitchat", create_vector_intent=False)
    assert not delivery.intents_exist(msg.id)


# ── receipts ──


def test_receipt_appears_after_index(sample_settings):
    sessions = SessionStore(sample_settings)
    mem = ConversationMemory(sample_settings, session_store=sessions)
    delivery = VectorDeliveryStore(sample_settings)

    user_msg, asst_msg = persist_turn(
        sessions=sessions, memory=mem, session_id="s1",
        user_content="question", assistant_content="answer",
        intent="rag", index_vector=True,
    )
    # Both messages should have receipts for the default profile
    assert delivery.has_receipt(user_msg.id, "default")
    assert delivery.has_receipt(asst_msg.id, "default")


def test_failed_upsert_leaves_intent_without_receipt(sample_settings):
    """If vector index fails, the intent exists but no receipt."""
    sessions = SessionStore(sample_settings)
    delivery = VectorDeliveryStore(sample_settings)

    # Create a message with intent but DON'T index
    msg = _add_msg(sessions, content="will not be indexed", create_vector_intent=True)
    assert delivery.intents_exist(msg.id)
    assert not delivery.has_receipt(msg.id, "default")


# ── replay queries ──


def test_source_records_through(sample_settings):
    sessions = SessionStore(sample_settings)
    delivery = VectorDeliveryStore(sample_settings)

    m1 = _add_msg(sessions, content="msg1", create_vector_intent=True)
    m2 = _add_msg(sessions, content="msg2", create_vector_intent=True)
    m3 = _add_msg(sessions, content="msg3", create_vector_intent=True)

    max_seq = delivery.max_seq()
    # Mark m1 as delivered
    delivery.mark_delivered([m1.id], "default")

    pending = delivery.source_records_through(max_seq, "default")
    assert m1.id not in pending
    assert m2.id in pending
    assert m3.id in pending


def test_pending_after(sample_settings):
    sessions = SessionStore(sample_settings)
    delivery = VectorDeliveryStore(sample_settings)

    m1 = _add_msg(sessions, content="old", create_vector_intent=True)
    seq_after_m1 = delivery.max_seq()
    m2 = _add_msg(sessions, content="new", create_vector_intent=True)

    pending = delivery.pending_after(seq_after_m1, "default")
    assert m1.id not in pending
    assert m2.id in pending


def test_bootstrap_receipts(sample_settings):
    sessions = SessionStore(sample_settings)
    delivery = VectorDeliveryStore(sample_settings)

    m1 = _add_msg(sessions, content="existing1", create_vector_intent=True)
    m2 = _add_msg(sessions, content="existing2", create_vector_intent=True)

    count = delivery.bootstrap_receipts("active-profile", [m1.id, m2.id])
    assert count == 2
    assert delivery.has_receipt(m1.id, "active-profile")
    assert delivery.has_receipt(m2.id, "active-profile")


def test_bootstrap_receipts_skips_existing(sample_settings):
    sessions = SessionStore(sample_settings)
    delivery = VectorDeliveryStore(sample_settings)

    m1 = _add_msg(sessions, content="existing", create_vector_intent=True)
    delivery.mark_delivered([m1.id], "p1")

    # Bootstrap to same profile should not duplicate
    count = delivery.bootstrap_receipts("p1", [m1.id])
    assert count == 0  # already exists


# ── persist_turn integration ──


def test_persist_turn_no_index(sample_settings):
    sessions = SessionStore(sample_settings)
    mem = ConversationMemory(sample_settings, session_store=sessions)
    delivery = VectorDeliveryStore(sample_settings)

    user_msg, asst_msg = persist_turn(
        sessions=sessions, memory=mem, session_id="s1",
        user_content="hi", assistant_content="hello",
        intent="direct", index_vector=False,
    )
    assert not delivery.intents_exist(user_msg.id)
    assert not delivery.intents_exist(asst_msg.id)
    assert not delivery.has_receipt(user_msg.id, "default")
