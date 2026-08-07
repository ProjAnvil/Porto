"""SessionStore 单元测试——纯 SQLite CRUD。"""
from porto_chatbot.memory.session_store import SessionStore, SessionSummary


def test_ensure_session_is_idempotent(sample_settings):
    store = SessionStore(sample_settings)
    s1 = store.ensure_session("s1")
    assert s1.id == "s1"
    assert s1.status == "active"
    assert s1.title is None
    s2 = store.ensure_session("s1")
    assert s2.id == "s1"
    # Should not duplicate
    store2 = SessionStore(sample_settings)
    s3 = store2.ensure_session("s1")
    assert s3.id == "s1"


def test_add_message_creates_session_and_touches(sample_settings):
    store = SessionStore(sample_settings)
    msg = store.add_message(
        session_id="auto", role="user", content="hello", intent="direct",
    )
    assert msg.session_id == "auto"
    assert msg.intent == "direct"
    assert msg.indexed is False
    session = store.get_session("auto")
    assert session is not None
    assert session.last_active_at >= session.created_at


def test_list_messages_returns_all_desc(sample_settings):
    store = SessionStore(sample_settings)
    store.add_message(session_id="s1", role="user", content="first", intent="direct")
    store.add_message(session_id="s1", role="assistant", content="second", intent="direct")
    msgs = store.list_messages("s1")
    assert len(msgs) == 2
    assert msgs[0].content == "second"  # DESC (new→old)
    assert msgs[1].content == "first"


def test_get_messages_ordered_asc(sample_settings):
    store = SessionStore(sample_settings)
    store.add_message(session_id="s1", role="user", content="first", intent="rag", indexed=False)
    store.add_message(session_id="s1", role="assistant", content="second", intent="rag", indexed=False)
    store.mark_indexed([m.id for m in store.list_messages("s1")])
    store.add_message(session_id="s1", role="user", content="chitchat", intent="direct", indexed=False)
    # All messages
    all_msgs = store.get_messages_ordered("s1")
    assert len(all_msgs) == 3
    assert all_msgs[0].content == "first"  # ASC
    # indexed_only filters out chitchat
    indexed = store.get_messages_ordered("s1", indexed_only=True)
    assert len(indexed) == 2
    assert all(m.indexed for m in indexed)


def test_list_sessions_with_title_and_preview(sample_settings):
    store = SessionStore(sample_settings)
    store.update_title("s1", "My Chat")
    store.add_message(session_id="s1", role="user", content="hello world this is a long preview message", intent="direct")
    items, total = store.list_sessions()
    assert total == 1
    assert items[0]["session_id"] == "s1"
    assert items[0]["title"] == "My Chat"
    assert items[0]["message_count"] == 1
    assert "hello world" in items[0]["preview"]
    assert items[0]["first_at"] is not None
    assert items[0]["last_at"] is not None


def test_claude_session_mapping(sample_settings):
    store = SessionStore(sample_settings)
    assert store.get_claude_session("p1") is None
    store.save_claude_session("p1", "claude-abc")
    assert store.get_claude_session("p1") == "claude-abc"
    store.save_claude_session("p1", "claude-xyz")  # update
    assert store.get_claude_session("p1") == "claude-xyz"


def test_summary_cache(sample_settings):
    store = SessionStore(sample_settings)
    assert store.get_summary("s1") is None
    store.save_summary("s1", "summary text", "msg-99")
    s = store.get_summary("s1")
    assert isinstance(s, SessionSummary)
    assert s.summary == "summary text"
    assert s.last_message_id == "msg-99"
