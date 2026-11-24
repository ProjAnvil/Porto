"""Regression test for stripping hnsw:space in the chroma update_metadata.

Background: chromadb forbids ``modify`` to carry the ``"hnsw:space"`` key (it raises
"Changing the distance function ..." even when the value is unchanged) — this always
triggered the first time a non-LOCAL embedding wrote embedding_dimensions (it once
blocked index building for the ollama semantic profile). Fix: the key is stripped from
the payload (this repository's chroma collection is always L2, the chromadb default).
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
    # Before the fix: merged contains hnsw:space (from the existing metadata) → modify
    # always raised
    store._repo.update_metadata(name, {"embedding_dimensions": 999})
    after = store._repo.get_metadata(name)
    assert after.get("embedding_dimensions") == 999


def test_update_metadata_noop_when_unchanged(tmp_path):
    """An unchanged value does not trigger modify (the guard still applies)."""
    store = _store(tmp_path, "reg_noop")
    name = store.settings.vector_collection
    dim = store.embeddings.probe_dimension()
    meta = store._expected_collection_metadata()
    store._repo.ensure_collection(
        CollectionSpec(name=name, dimension=dim, metric=DistanceMetric.L2, metadata=meta)
    )
    # Update again with the same value: no exception, value unchanged
    store._repo.update_metadata(name, meta)
    assert store._repo.get_metadata(name).get("embedding_dimensions") == 128
