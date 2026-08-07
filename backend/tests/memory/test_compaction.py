from __future__ import annotations

from unittest.mock import MagicMock

from porto_chatbot.memory.compaction import get_compacted_history, summarize_records
from porto_chatbot.memory.session_store import SessionStore
from porto_chatbot.models import MemoryRecord


def _record(role, content):
    return MemoryRecord(id="x", session_id="s", role=role, content=content, created_at="t")


def test_summarize_prompt_preserves_entities():
    """a1:摘要 system prompt 必须包含实体保留要求。"""
    llm = MagicMock()
    llm.enabled = True
    llm.complete.return_value = "摘要"
    records = [_record("user", "用 OAuth 2.0"), _record("assistant", "好的")]
    summarize_records(records, llm)
    system_arg = llm.complete.call_args.args[0]
    assert "专有名词" in system_arg
    assert "变量名" in system_arg
    assert "待澄清" in system_arg
    assert "已确认" in system_arg or "已否决" in system_arg


def test_summarize_skips_when_llm_disabled():
    llm = MagicMock()
    llm.enabled = False
    out = summarize_records([_record("user", "x")], llm)
    assert out == ""
    llm.complete.assert_not_called()


def test_summarize_empty_records():
    llm = MagicMock()
    llm.enabled = True
    assert summarize_records([], llm) == ""
    llm.complete.assert_not_called()


def test_compaction_only_uses_indexed_messages(sample_settings):
    """indexed_only=True: chitchat (indexed=False) 被排除出 compaction。"""
    store = SessionStore(sample_settings)
    # Add 25 indexed messages (above threshold=20)
    for i in range(25):
        msg = store.add_message(
            session_id="s1", role="user" if i % 2 == 0 else "assistant",
            content=f"rag message {i}", intent="rag", indexed=False,
        )
    store.mark_indexed([m.id for m in store.list_messages("s1")])
    # Add 5 chitchat messages (not indexed)
    for i in range(5):
        store.add_message(
            session_id="s1", role="user",
            content=f"chitchat {i}", intent="direct", indexed=False,
        )
    summary, recent = get_compacted_history("s1", store, llm=None)
    # LLM disabled → returns ("", recent). recent should only contain indexed messages.
    # Without LLM, threshold logic still runs: total indexed = 25 > 20 → keep_recent
    assert len(recent) <= store.settings.memory_recent_keep
    assert all(r.indexed for r in recent)  # no chitchat in recent
