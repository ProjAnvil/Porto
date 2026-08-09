"""ConversationMemory unit tests — repository-backed vector ops + SQLite hydration."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from porto_chatbot.memory.conversation_memory import ConversationMemory
from porto_chatbot.memory.session_store import SessionStore
from porto_chatbot.models import MessageRecord
from porto_chatbot.vector_stores import DimensionMismatchError

# ── helpers ──


def _add_msg(
    sessions: SessionStore, *,
    session_id: str = "s1", role: str = "user",
    content: str = "hello", intent: str = "rag",
) -> MessageRecord:
    """Add a message to SQLite and return the record."""
    return sessions.add_message(
        session_id=session_id, role=role, content=content, intent=intent,
    )


# ── round-trip & hydration ──


def test_index_and_search_roundtrip(sample_settings):
    sessions = SessionStore(sample_settings)
    mem = ConversationMemory(sample_settings, session_store=sessions)
    msg = _add_msg(sessions, content="payment platform architecture")
    mem.index([msg])

    results = mem.search("payment platform architecture", session_id="s1", top_k=5)
    assert len(results) >= 1
    # Text is hydrated from SQLite, not from the vector store
    assert "payment" in results[0].text.lower()


def test_search_hydrates_from_sqlite(sample_settings):
    """Search must return SQLite-canonical text, not vector-store document."""
    sessions = SessionStore(sample_settings)
    mem = ConversationMemory(sample_settings, session_store=sessions)
    msg = _add_msg(sessions, content="original content here")
    mem.index([msg])

    results = mem.search("original content", session_id="s1", top_k=5)
    assert results
    assert results[0].text == "original content here"
    assert results[0].id == msg.id


def test_search_omits_missing_sqlite_rows(sample_settings):
    """If a message is deleted from SQLite but its vector lingers, omit it."""
    sessions = SessionStore(sample_settings)
    mem = ConversationMemory(sample_settings, session_store=sessions)
    msg = _add_msg(sessions, content="will be deleted")
    mem.index([msg])

    # Delete from SQLite (vector still in collection)
    with sessions._conn() as conn:
        conn.execute("DELETE FROM messages WHERE id=?", (msg.id,))
        conn.commit()

    results = mem.search("will be deleted", session_id="s1", top_k=5)
    assert all(r.id != msg.id for r in results)


# ── session isolation ──


def test_session_isolation(sample_settings):
    sessions = SessionStore(sample_settings)
    mem = ConversationMemory(sample_settings, session_store=sessions)
    msg_a = _add_msg(sessions, session_id="A", content="alpha topic")
    msg_b = _add_msg(sessions, session_id="B", content="beta topic")
    mem.index([msg_a, msg_b])

    results_a = mem.search("alpha", session_id="A")
    results_b = mem.search("alpha", session_id="B")
    assert any("alpha" in r.text.lower() for r in results_a)
    assert not any("alpha" in r.text.lower() for r in results_b)


# ── count ──


def test_count(sample_settings):
    sessions = SessionStore(sample_settings)
    mem = ConversationMemory(sample_settings, session_store=sessions)
    assert mem.count() == 0

    msg1 = _add_msg(sessions, content="x")
    msg2 = _add_msg(sessions, content="y")
    mem.index([msg1, msg2])
    sessions.mark_indexed([msg1.id, msg2.id])

    assert mem.count() == 2
    assert mem.count(session_id="s1") == 2
    assert mem.count(session_id="other") == 0


# ── reset ──


def test_reset(sample_settings):
    sessions = SessionStore(sample_settings)
    mem = ConversationMemory(sample_settings, session_store=sessions)
    msg = _add_msg(sessions, content="x")
    mem.index([msg])
    assert mem.count() == 1

    mem.reset()
    assert mem.count() == 0


# ── empty collection ──


def test_empty_collection_search_returns_empty(sample_settings):
    mem = ConversationMemory(sample_settings)
    results = mem.search("anything", session_id="s1")
    assert results == []


# ── dimension mismatch ──


def test_search_dimension_mismatch_returns_empty(sample_settings):
    """Typed dimension mismatch on search degrades to empty without reset."""
    sessions = SessionStore(sample_settings)
    mem = ConversationMemory(sample_settings, session_store=sessions)
    msg = _add_msg(sessions, content="dimension test")
    mem.index([msg])
    assert mem.count() == 1  # vector exists before mismatch

    # Query with wrong dimension → DimensionMismatchError → empty result
    with patch.object(mem.embeddings, "embed_query", return_value=[0.0] * 64):
        results = mem.search("dimension test", session_id="s1")
    assert results == []
    # Collection was NOT reset — vector still present
    assert mem.count() == 1


def test_index_dimension_mismatch_leaves_sqlite(sample_settings):
    """Dimension mismatch on index leaves SQLite message, no vector written."""
    sessions = SessionStore(sample_settings)
    mem = ConversationMemory(sample_settings, session_store=sessions)
    msg = _add_msg(sessions, content="will fail to index")

    wrong_embedding = [[0.0] * 64]
    with (
        patch.object(mem.embeddings, "embed_documents", return_value=wrong_embedding),
        pytest.raises(DimensionMismatchError),
    ):
        mem.index([msg])

    # Message present in SQLite, not indexed
    assert sessions.count_indexed("s1") == 0
    assert mem.count() == 0
