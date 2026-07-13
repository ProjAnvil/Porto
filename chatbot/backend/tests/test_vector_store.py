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
    assert results[0].path == "kb/payment-platform.md"
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


def test_build_multi_root_same_filename(tmp_path):
    """多目录同名文件：chunk id 含 root 前缀防冲突，两个都保留，path 带 root.name。"""
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
    assert stats.documents == 2  # 两个同名文件都建索引（id 防冲突）

    results = store.search("支付", top_k=5)
    paths = {r.metadata.get("path") for r in results}
    assert any("kb1/" in (p or "") for p in paths)
    results2 = store.search("风控", top_k=5)
    paths2 = {r.metadata.get("path") for r in results2}
    assert any("kb2/" in (p or "") for p in paths2)


def test_search_retrieval_methods(sample_settings):
    """vector / bm25 / hybrid 三种检索算法各自可用。"""
    store = LocalVectorStore(sample_settings)
    store.build()

    sample_settings.retrieval_method = "vector"
    vec = store.search("支付 退款", top_k=2)
    sample_settings.retrieval_method = "bm25"
    bm = store.search("支付 退款", top_k=2)
    sample_settings.retrieval_method = "hybrid"
    hy = store.search("支付 退款", top_k=2)

    assert vec and bm and hy
    sample_settings.retrieval_method = "hybrid"  # 复位
