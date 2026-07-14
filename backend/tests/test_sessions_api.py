from __future__ import annotations

from porto_chatbot.memory.store import MemoryStore


def _store(sample_settings):
    return MemoryStore(sample_settings)


def test_list_sessions_aggregates_by_session(sample_settings):
    s = _store(sample_settings)
    s.add(session_id="s1", role="user", content="hello")
    s.add(session_id="s1", role="assistant", content="hi there")
    s.add(session_id="s2", role="user", content="another session")
    items, total = s.list_sessions(limit=20, offset=0)
    assert total == 2
    # 按 last_at 倒序：s2 后加 → 在前
    assert items[0]["session_id"] == "s2"
    assert items[1]["session_id"] == "s1"
    assert items[1]["message_count"] == 2
    assert items[1]["first_at"] is not None
    assert items[1]["last_at"] is not None
    # preview = 最后一条消息
    assert items[1]["preview"] == "hi there"


def test_list_sessions_pagination(sample_settings):
    s = _store(sample_settings)
    for i in range(5):
        s.add(session_id=f"s{i}", role="user", content=f"msg {i}")
    items, total = s.list_sessions(limit=2, offset=0)
    assert total == 5
    assert len(items) == 2
    items2, _ = s.list_sessions(limit=2, offset=2)
    assert len(items2) == 2
    # 无重叠
    ids = {i["session_id"] for i in items} | {i["session_id"] for i in items2}
    assert len(ids) == 4


def test_list_sessions_date_filter(sample_settings):
    s = _store(sample_settings)
    s.add(session_id="s1", role="user", content="msg")
    # date 过滤 last_at 所在日期；用今天日期应能匹配刚加的
    from datetime import UTC, datetime
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    items, total = s.list_sessions(date=today, limit=20, offset=0)
    assert total == 1
    assert items[0]["session_id"] == "s1"
    # 用一个不存在的日期
    items_empty, total_empty = s.list_sessions(date="2099-01-01", limit=20, offset=0)
    assert total_empty == 0
    assert len(items_empty) == 0


def test_list_sessions_date_filter_uses_last_at(sample_settings):
    """spec: date 过滤 last_at 所在日期。多日期 session 按非最后日期不应匹配。"""
    import sqlite3
    from porto_chatbot.memory.store import MemoryStore
    s = MemoryStore(sample_settings)
    r1 = s.add(session_id="s1", role="user", content="day1 msg")
    r2 = s.add(session_id="s1", role="assistant", content="day2 msg")
    # 改 created_at 模拟跨日期（s1 的 last_at = 2026-07-15）
    with sqlite3.connect(s.settings.memory_db_path) as conn:
        conn.execute("UPDATE memories SET created_at=? WHERE id=?", ("2026-07-13T10:00:00+00:00", r1.id))
        conn.execute("UPDATE memories SET created_at=? WHERE id=?", ("2026-07-15T10:00:00+00:00", r2.id))
    # 按 last_at 日期(07-15)过滤 → 应匹配 s1
    items, total = s.list_sessions(date="2026-07-15", limit=20, offset=0)
    assert total == 1
    assert items[0]["session_id"] == "s1"
    # 按非最后日期(07-13)过滤 → 不应匹配 s1（last_at 是 07-15）
    items, total = s.list_sessions(date="2026-07-13", limit=20, offset=0)
    assert total == 0
