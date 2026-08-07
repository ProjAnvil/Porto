"""persist_turn / index_and_mark / maybe_generate_title 测试。"""
from unittest.mock import MagicMock, patch

from porto_chatbot.memory.conversation_memory import ConversationMemory
from porto_chatbot.memory.persist import (
    maybe_generate_title,
    persist_turn,
)
from porto_chatbot.memory.session_store import SessionStore


def test_persist_turn_no_index(sample_settings):
    sessions = SessionStore(sample_settings)
    memory = ConversationMemory(sample_settings)
    user_msg, asst_msg = persist_turn(
        sessions=sessions, memory=memory, session_id="s1",
        user_content="hi", assistant_content="hello",
        intent="direct", index_vector=False,
    )
    assert user_msg.role == "user"
    assert asst_msg.role == "assistant"
    assert user_msg.indexed is False
    assert memory.count() == 0  # not indexed


def test_persist_turn_with_index(sample_settings):
    sessions = SessionStore(sample_settings)
    memory = ConversationMemory(sample_settings)
    user_msg, asst_msg = persist_turn(
        sessions=sessions, memory=memory, session_id="s1",
        user_content="what is payment", assistant_content="payment service",
        intent="rag", index_vector=True,
    )
    assert user_msg.indexed is True
    assert asst_msg.indexed is True
    assert memory.count() == 2


def test_persist_turn_index_failure_graceful(sample_settings):
    sessions = SessionStore(sample_settings)
    memory = ConversationMemory(sample_settings)
    # Force index to raise
    with patch.object(memory, "index", side_effect=RuntimeError("boom")):
        user_msg, asst_msg = persist_turn(
            sessions=sessions, memory=memory, session_id="s1",
            user_content="q", assistant_content="a",
            intent="rag", index_vector=True,
        )
    # Messages persisted to SQLite, but indexed stays False
    assert user_msg.indexed is False
    assert asst_msg.indexed is False
    msgs = sessions.list_messages("s1")
    assert len(msgs) == 2


def test_maybe_generate_title_skips_if_title_exists(sample_settings):
    sessions = SessionStore(sample_settings)
    sessions.ensure_session("s1")  # Must create session first
    sessions.update_title("s1", "Existing")
    llm = MagicMock()
    maybe_generate_title(sessions, llm, "s1", "first message")
    llm.complete.assert_not_called()


def test_maybe_generate_title_generates_for_new_session(sample_settings):
    sessions = SessionStore(sample_settings)
    sessions.add_message(session_id="s1", role="user", content="hello", intent="direct")
    llm = MagicMock()
    llm.complete.return_value = "Generated Title"
    maybe_generate_title(sessions, llm, "s1", "hello")
    # Thread is fire-and-forget; wait a moment for it
    import time
    time.sleep(0.5)
    session = sessions.get_session("s1")
    assert session.title == "Generated Title"
