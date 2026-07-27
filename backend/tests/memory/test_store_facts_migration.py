from __future__ import annotations

import sqlite3

from porto_chatbot.settings import Settings


def test_session_facts_table_created(tmp_path):
    settings = Settings(data_dir=tmp_path)
    # 触发 _init_db(MemoryStore.__init__ 会调)
    from porto_chatbot.memory.store import MemoryStore

    MemoryStore(settings)
    with sqlite3.connect(settings.memory_db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(session_facts)").fetchall()}
        assert cols == {
            "id", "session_id", "category", "content",
            "status", "source_msg_id", "created_at", "updated_at",
        }
        idx = {row[1] for row in conn.execute("PRAGMA index_list(session_facts)").fetchall()}
        assert "idx_facts_session" in idx
        assert "idx_facts_session_cat" in idx
