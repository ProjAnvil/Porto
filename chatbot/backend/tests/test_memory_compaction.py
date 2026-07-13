from __future__ import annotations

from porto_chatbot.llm import LLMClient
from porto_chatbot.memory import MemoryStore, get_compacted_history, summarize_records
from porto_chatbot.settings import Settings


def _enabled_llm(tmp_path) -> LLMClient:
    s = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "d",
        log_dir=tmp_path / "l",
        agent_provider="openai",
        agent_model="m",
    )
    s.agent_api_key = "k"
    return LLMClient(s)


def _add_n(store: MemoryStore, session_id: str, n: int) -> None:
    for i in range(n):
        store.add(
            session_id=session_id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"msg-{i}",
        )


def _store(sample_settings, **overrides) -> MemoryStore:
    for key, value in overrides.items():
        setattr(sample_settings, key, value)
    return MemoryStore(sample_settings)


def test_compaction_below_threshold_returns_all(sample_settings):
    store = _store(sample_settings, memory_compact_threshold=10, memory_recent_keep=2)
    _add_n(store, "s1", 5)
    summary, records = get_compacted_history("s1", store, llm=None)
    assert summary == ""
    assert len(records) == 5  # 未超阈值，全部原文


def test_compaction_disabled_llm_keeps_only_recent(sample_settings):
    store = _store(sample_settings, memory_compact_threshold=4, memory_recent_keep=2)
    _add_n(store, "s1", 6)
    summary, recent = get_compacted_history("s1", store, llm=None)
    assert summary == ""  # 无 LLM 无法摘要
    assert len(recent) == 2  # 降级：只给近期原文


def test_compaction_with_llm_summarizes_and_caches(sample_settings, monkeypatch, tmp_path):
    store = _store(sample_settings, memory_compact_threshold=4, memory_recent_keep=2)
    _add_n(store, "s1", 6)
    llm = _enabled_llm(tmp_path)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "历史摘要文本")
    summary, recent = get_compacted_history("s1", store, llm)
    assert summary == "历史摘要文本"
    assert len(recent) == 2
    cached = store.get_summary("s1")
    assert cached is not None
    assert cached["summary"] == "历史摘要文本"


def test_compaction_cache_hit_skips_llm(sample_settings, monkeypatch, tmp_path):
    store = _store(sample_settings, memory_compact_threshold=4, memory_recent_keep=2)
    _add_n(store, "s1", 6)
    llm = _enabled_llm(tmp_path)
    calls = {"n": 0}

    def fake_complete(*a, **k):
        calls["n"] += 1
        return "摘要"

    monkeypatch.setattr(llm, "complete", fake_complete)
    get_compacted_history("s1", store, llm)  # 首次：调 LLM
    assert calls["n"] == 1
    get_compacted_history("s1", store, llm)  # 缓存命中：不调
    assert calls["n"] == 1


def test_compaction_resummarizes_when_old_set_grows(sample_settings, monkeypatch, tmp_path):
    store = _store(sample_settings, memory_compact_threshold=4, memory_recent_keep=2)
    _add_n(store, "s1", 6)
    llm = _enabled_llm(tmp_path)
    calls = []
    monkeypatch.setattr(llm, "complete", lambda *a, **k: calls.append(1) or "摘要")
    get_compacted_history("s1", store, llm)
    _add_n(store, "s1", 4)  # 再加 4 条，old 集合改变 → last_old_id 变化
    summary, recent = get_compacted_history("s1", store, llm)
    assert len(calls) == 2  # 重新摘要
    assert len(recent) == 2


def test_summarize_records_disabled_returns_empty(sample_settings):
    store = _store(sample_settings)
    _add_n(store, "s1", 3)
    records = store.get_messages_ordered("s1")
    assert summarize_records(records, llm=None) == ""
