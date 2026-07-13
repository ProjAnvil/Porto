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


def test_ensure_index_no_rebuild_on_dimension_change(sample_settings):
    """新设计：维度变化后 ensure_index 不再自动重建（重建由 IndexSupervisor 手动触发）；
    search 因维度不匹配直接返回空。"""
    LocalVectorStore(sample_settings).build()  # 128 维

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

    assert stats.embedding_dimensions == 128  # 旧维度，未重建
    assert results == []  # 维度不匹配 → 不可用，返回空
