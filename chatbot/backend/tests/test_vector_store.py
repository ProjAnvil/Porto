from __future__ import annotations

from porto_chatbot.settings import Settings
from porto_chatbot.vector_store import LocalVectorStore


def test_build_and_search(sample_settings):
    store = LocalVectorStore(sample_settings)
    stats = store.build()
    assert stats.documents == 1
    assert stats.chunks >= 1

    results = store.search("支付 风控 refund", top_k=2)
    assert results
    assert results[0].path == "payment-platform.md"
    assert results[0].score > 0


def test_ensure_index_rebuilds_when_embedding_dimension_changes(sample_settings):
    first_store = LocalVectorStore(sample_settings)
    first_stats = first_store.build()
    assert first_stats.embedding_dimensions == 128

    changed_settings = Settings(
        kb_path=sample_settings.kb_path,
        data_dir=sample_settings.data_dir,
        log_dir=sample_settings.log_dir,
        embedding_dimensions=64,
        embedding_provider="local",
    )
    changed_store = LocalVectorStore(changed_settings)

    rebuilt_stats = changed_store.ensure_index()
    results = changed_store.search("支付 风控 refund", top_k=2)

    assert rebuilt_stats.embedding_dimensions == 64
    assert results
