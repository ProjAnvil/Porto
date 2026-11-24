from __future__ import annotations

from porto_chatbot.memory.session_store import SessionStore


def _store(sample_settings):
    return SessionStore(sample_settings)


def test_list_sessions_aggregates_by_session(sample_settings):
    s = _store(sample_settings)
    s.add_message(session_id="s1", role="user", content="hello")
    s.add_message(session_id="s1", role="assistant", content="hi there")
    s.add_message(session_id="s2", role="user", content="another session")
    items, total = s.list_sessions(limit=20, offset=0)
    assert total == 2
    # Sorted by last_at descending: s2 was added later → listed first
    assert items[0]["session_id"] == "s2"
    assert items[1]["session_id"] == "s1"
    assert items[1]["message_count"] == 2
    assert items[1]["first_at"] is not None
    assert items[1]["last_at"] is not None
    # preview = the last message
    assert items[1]["preview"] == "hi there"


def test_list_sessions_pagination(sample_settings):
    s = _store(sample_settings)
    for i in range(5):
        s.add_message(session_id=f"s{i}", role="user", content=f"msg {i}")
    items, total = s.list_sessions(limit=2, offset=0)
    assert total == 5
    assert len(items) == 2
    items2, _ = s.list_sessions(limit=2, offset=2)
    assert len(items2) == 2
    # No overlap
    ids = {i["session_id"] for i in items} | {i["session_id"] for i in items2}
    assert len(ids) == 4


def test_list_sessions_date_filter(sample_settings):
    s = _store(sample_settings)
    s.add_message(session_id="s1", role="user", content="msg")
    # The date filter matches the calendar date of last_at; today's date should match the rows
    # just added
    from datetime import UTC, datetime
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    items, total = s.list_sessions(date=today, limit=20, offset=0)
    assert total == 1
    assert items[0]["session_id"] == "s1"
    # Use a date with no sessions
    items_empty, total_empty = s.list_sessions(date="2099-01-01", limit=20, offset=0)
    assert total_empty == 0
    assert len(items_empty) == 0


def test_list_sessions_date_filter_uses_last_at(sample_settings):
    """Spec: the date filter matches the calendar date of last_at. A session spanning several
    dates must not match a non-last date."""
    import sqlite3

    s = SessionStore(sample_settings)
    s.add_message(session_id="s1", role="user", content="day1 msg")
    s.add_message(session_id="s1", role="assistant", content="day2 msg")
    # Move last_active_at to simulate a session spanning dates (last_at of s1 = 2026-07-15)
    with sqlite3.connect(s.settings.memory_db_path) as conn:
        conn.execute("UPDATE sessions SET last_active_at=? WHERE id=?", ("2026-07-15T10:00:00+00:00", "s1"))
    # Filter by the last_at date (07-15) → should match s1
    items, total = s.list_sessions(date="2026-07-15", limit=20, offset=0)
    assert total == 1
    assert items[0]["session_id"] == "s1"
    # Filter by a non-last date (07-13) → must not match s1 (its last_at is 07-15)
    items, total = s.list_sessions(date="2026-07-13", limit=20, offset=0)
    assert total == 0
