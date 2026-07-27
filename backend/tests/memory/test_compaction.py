from __future__ import annotations

from unittest.mock import MagicMock

from porto_chatbot.memory.compaction import summarize_records
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
