from __future__ import annotations

import pytest

from porto_chatbot.models.enums import QueryTransformStrategy
from porto_chatbot.query_transform import TransformResult, retrieve_with_transform
from porto_chatbot.settings import Settings
from porto_chatbot.vector_store import LocalVectorStore


@pytest.fixture()
def store_with_docs(sample_settings: Settings) -> LocalVectorStore:
    """Build a store with indexed documents, shared by the query_transform tests.

    Same shape as the fixture of the same name in ``test_vector_store.py``; defined
    independently here so the Task 4 test code stays untouched. pytest fixtures are
    isolated per module, so the two do not conflict.
    """
    store = LocalVectorStore(sample_settings)
    store.build()
    return store


def test_none_strategy_equals_store_search(store_with_docs):
    """The NONE strategy goes through the original store.search path with no behavior change."""
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
    """LLM unavailable or raising → fail open: fall back to _search_raw with degraded=True.

    In Task 5, _generate_hypothetical is a NotImplementedError stub; the exception is
    caught by the try/except in ``retrieve_with_transform``, which exercises the fallback
    path.
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
    # Still returns the base retrieval results without crashing
    assert isinstance(result.chunks, list)


def test_hyde_uses_fake_doc_for_search(store_with_docs, monkeypatch):
    """HyDE: retrieves with an LLM-generated hypothetical answer instead of the original query."""
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
    assert "假设性" in captured["query_used"]  # retrieved with the hypothetical answer, not the original query


def test_multi_query_fuses_variants(store_with_docs, monkeypatch):
    """Multi-Query: each of the 3 rewritten variants is retrieved once, then fused with RRF."""
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
    assert len(calls) == 3  # one retrieval per rewrite


def test_decomposition_splits_and_merges(store_with_docs, monkeypatch):
    """Decomposition: split into 2 sub-questions, retrieve each, then merge_dedupe."""
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
