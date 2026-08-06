from __future__ import annotations

import pytest

from porto_chatbot.models.enums import QueryTransformStrategy
from porto_chatbot.query_transform import TransformResult, retrieve_with_transform
from porto_chatbot.settings import Settings
from porto_chatbot.vector_store import LocalVectorStore


@pytest.fixture()
def store_with_docs(sample_settings: Settings) -> LocalVectorStore:
    """构造已索引文档的 store，供 query_transform 测试复用。

    与 ``test_vector_store.py`` 中的同名 fixture 同形；此处本文件内独立定义，
    避免修改 Task 4 测试代码。pytest 的 fixture 按 module 隔离，互不冲突。
    """
    store = LocalVectorStore(sample_settings)
    store.build()
    return store


def test_none_strategy_equals_store_search(store_with_docs):
    """NONE 策略走 store.search 原路径，行为零变化。"""
    store = store_with_docs
    expected = store.search("查询", top_k=3)
    result = retrieve_with_transform(
        "查询", QueryTransformStrategy.NONE, store, store.settings, llm=None, top_k=3
    )
    assert isinstance(result, TransformResult)
    assert result.degraded is False
    assert result.degrade_reason == ""
    assert [r.id for r in result.chunks] == [r.id for r in expected]


def test_hyde_fallback_on_llm_failure(store_with_docs, monkeypatch):
    """LLM 不可用/抛异常 → fail-open 回退 _search_raw + degraded=True。

    Task 5 中 _generate_hypothetical 是 NotImplementedError 桩；该异常被
    ``retrieve_with_transform`` 的 try/except 捕获，验证降级路径。
    """
    store = store_with_docs

    class BoomLLM:
        enabled = True

        def complete(self, *a, **k):
            raise RuntimeError("timeout")

    result = retrieve_with_transform(
        "查询", QueryTransformStrategy.HYDE, store, store.settings, llm=BoomLLM(), top_k=3
    )
    assert result.degraded is True
    assert "llm_call_failed" in result.degrade_reason
    # 仍返回基础检索结果，不崩
    assert isinstance(result.chunks, list)


def test_hyde_uses_fake_doc_for_search(store_with_docs, monkeypatch):
    """HyDE：用 LLM 生成的假答案（而非原 query）做检索。"""
    store = store_with_docs
    captured = {}

    class FakeLLM:
        enabled = True

        def complete(self, system, user):
            captured["called"] = True
            return "这是一个假设性的答案文档，描述了支付风控的架构。"

    monkeypatch.setattr(
        store, "_search_raw", lambda q, top_k: captured.setdefault("query_used", q) or []
    )
    result = retrieve_with_transform(
        "支付风控", QueryTransformStrategy.HYDE, store, store.settings, FakeLLM(), top_k=3
    )
    assert result.degraded is False
    assert "假设性" in captured["query_used"]  # 用假答案而非原 query 检索


def test_multi_query_fuses_variants(store_with_docs, monkeypatch):
    """Multi-Query：3 个改写变体各检索一次，再 RRF 融合。"""
    store = store_with_docs
    calls = []

    class FakeLLM:
        enabled = True

        def complete(self, system, user):
            return "改写1\n改写2\n改写3"

    monkeypatch.setattr(store, "_search_raw", lambda q, top_k: calls.append(q) or [])
    retrieve_with_transform(
        "查询", QueryTransformStrategy.MULTI_QUERY, store, store.settings, FakeLLM(), top_k=3
    )
    assert len(calls) == 3  # 3 个改写各检索一次


def test_decomposition_splits_and_merges(store_with_docs, monkeypatch):
    """Decomposition：拆成 2 个子问题分别检索，再 merge_dedupe。"""
    store = store_with_docs
    calls = []

    class FakeLLM:
        enabled = True

        def complete(self, system, user):
            return "子问题1\n子问题2"

    monkeypatch.setattr(store, "_search_raw", lambda q, top_k: calls.append(q) or [])
    retrieve_with_transform(
        "复杂问题",
        QueryTransformStrategy.DECOMPOSITION,
        store,
        store.settings,
        FakeLLM(),
        top_k=3,
    )
    assert len(calls) == 2
