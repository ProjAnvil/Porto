from __future__ import annotations

import pytest

from porto_chatbot.settings import Settings
from porto_chatbot.vector_store import LocalVectorStore


def test_build_and_search(sample_settings):
    store = LocalVectorStore(sample_settings)
    stats = store.build()
    assert stats.documents == 1
    assert stats.chunks >= 1

    results = store.search("支付 风控 refund", top_k=2)
    assert results
    assert results[0].path == "kb/payment-platform.md"
    assert results[0].score > 0


@pytest.fixture()
def store_with_docs(sample_settings):
    """Build a store with indexed documents, shared by the search/_search_raw tests."""
    store = LocalVectorStore(sample_settings)
    store.build()
    return store


def test_search_raw_no_rerank(store_with_docs):
    """_search_raw behaves the same as search with rerank disabled (behavior-preservation
    regression test)."""
    store = store_with_docs
    store.settings.rerank_enabled = False
    raw = store._search_raw("查询", top_k=3)
    full = store.search("查询", top_k=3)
    # With rerank off the two agree
    assert [r.id for r in raw] == [r.id for r in full]
    assert len(raw) <= 3


def test_ensure_index_no_rebuild_on_dimension_change(sample_settings):
    """New design: after the embedding dimensions change, ensure_index no longer rebuilds
    automatically (the rebuild is triggered manually through IndexSupervisor); search
    returns empty because of the dimension mismatch."""
    LocalVectorStore(sample_settings).build()  # 128 dimensions

    changed_settings = Settings(
        kb_dirs=[sample_settings.kb_path],
        data_dir=sample_settings.data_dir,
        log_dir=sample_settings.log_dir,
        embedding_dimensions=64,
        embedding_provider="local",
    )
    changed_store = LocalVectorStore(changed_settings)

    stats = changed_store.ensure_index()
    results = changed_store.search("支付 风控 refund", top_k=2)

    assert stats.embedding_dimensions == 128  # old dimensions, not rebuilt
    assert results == []  # dimension mismatch → unavailable, returns empty


def test_build_multi_root_same_filename(tmp_path):
    """Same file name in several directories: the chunk id carries a root prefix to avoid
    clashes, both are kept, and path includes root.name."""
    a = tmp_path / "kb1"
    b = tmp_path / "kb2"
    a.mkdir()
    b.mkdir()
    (a / "dup.md").write_text("# alpha\n支付内容一", encoding="utf-8")
    (b / "dup.md").write_text("# beta\n风控内容二", encoding="utf-8")
    s = Settings(
        kb_dirs=[a, b],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        embedding_dimensions=128,
        embedding_provider="local",
    )
    store = LocalVectorStore(s)
    stats = store.build()
    assert stats.documents == 2  # both same-named files are indexed (ids do not clash)

    results = store.search("支付", top_k=5)
    paths = {r.metadata.get("path") for r in results}
    assert any("kb1/" in (p or "") for p in paths)
    results2 = store.search("风控", top_k=5)
    paths2 = {r.metadata.get("path") for r in results2}
    assert any("kb2/" in (p or "") for p in paths2)


def test_search_retrieval_methods(sample_settings):
    """The three retrieval methods (vector / bm25 / hybrid) each work on their own."""
    store = LocalVectorStore(sample_settings)
    store.build()

    sample_settings.retrieval_method = "vector"
    vec = store.search("支付 退款", top_k=2)
    sample_settings.retrieval_method = "bm25"
    bm = store.search("支付 退款", top_k=2)
    sample_settings.retrieval_method = "hybrid"
    hy = store.search("支付 退款", top_k=2)

    assert vec and bm and hy
    sample_settings.retrieval_method = "hybrid"  # reset


def test_stats_tolerates_concurrent_collection_drop(monkeypatch, sample_settings):
    """F4: when GET /api/kb/stats races with the reindex reset (_reset_collection deletes the
    collection), repo.count() may raise. That must be tolerated and reported as chunks=0,
    not surface as a 500."""
    store = LocalVectorStore(sample_settings)

    def _raise_count(name):
        raise Exception("Collection dropped by concurrent reset")

    monkeypatch.setattr(store._repo, "count", _raise_count)
    assert store.stats().chunks == 0
    assert store.ensure_index().chunks == 0
