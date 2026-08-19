"""chroma update_metadata 的 hnsw:space 剥离回归测试。

背景：chromadb 禁止 ``modify`` 携带 ``"hnsw:space"`` 键（即使值未变也 raise
"Changing the distance function ..."）——非 LOCAL embedding 首次写入
embedding_dimensions 时必触发（曾阻断 ollama semantic profile 的索引构建）。
修复：payload 剥离该键（本仓库 chroma collection 恒 L2 == chromadb 默认）。
"""
from __future__ import annotations

from pathlib import Path

from porto_chatbot.settings import Settings
from porto_chatbot.vector_store import ChromaVectorStore
from porto_chatbot.vector_stores import CollectionSpec, DistanceMetric


def _store(tmp_path: Path, name: str) -> ChromaVectorStore:
    kb = tmp_path / "kb"
    kb.mkdir(exist_ok=True)
    (kb / "a.md").write_text("中国人民大学文学院介绍。" * 20, encoding="utf-8")
    s = Settings(
        kb_dirs=[kb], data_dir=tmp_path / "data", log_dir=tmp_path / "logs",
        vector_collection=name, embedding_provider="local", embedding_dimensions=128,
    )
    return ChromaVectorStore(s)


def test_update_metadata_strips_hnsw_space(tmp_path):
    store = _store(tmp_path, "reg_hnsw")
    name = store.settings.vector_collection
    dim = store.embeddings.probe_dimension()
    store._repo.ensure_collection(
        CollectionSpec(
            name=name, dimension=dim, metric=DistanceMetric.L2,
            metadata=store._expected_collection_metadata(),
        )
    )
    # 修复前：merged 含 hnsw:space（来自既有 metadata）→ modify 必 raise
    store._repo.update_metadata(name, {"embedding_dimensions": 999})
    after = store._repo.get_metadata(name)
    assert after.get("embedding_dimensions") == 999


def test_update_metadata_noop_when_unchanged(tmp_path):
    """值无变化时不触发 modify（守卫仍生效）。"""
    store = _store(tmp_path, "reg_noop")
    name = store.settings.vector_collection
    dim = store.embeddings.probe_dimension()
    meta = store._expected_collection_metadata()
    store._repo.ensure_collection(
        CollectionSpec(name=name, dimension=dim, metric=DistanceMetric.L2, metadata=meta)
    )
    # 同值再 update：不抛错、值不变
    store._repo.update_metadata(name, meta)
    assert store._repo.get_metadata(name).get("embedding_dimensions") == 128
