"""Tests for immutable KB generation tracking."""
from __future__ import annotations

from pathlib import Path

from porto_chatbot.bm25_index import Bm25Registry
from porto_chatbot.kb_generation_store import KbGenerationStore
from porto_chatbot.settings import Settings
from porto_chatbot.vector_store import LocalVectorStore


def test_generation_1_bootstraps_legacy_names(tmp_path):
    """Bootstrap creates gen 1 with unsuffixed collection name and legacy BM25 path."""
    s = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        embedding_dimensions=128,
        embedding_provider="local",
    )
    store = KbGenerationStore(s)
    gen = store.bootstrap()
    assert gen is not None
    assert gen.id == 1
    assert gen.collection_name == s.vector_collection  # "porto_kb"
    assert gen.bm25_path == str(s.data_dir / "bm25s_index")
    assert gen.status == "active"

    # Second bootstrap is a no-op
    assert store.bootstrap() is None


def test_allocate_creates_suffixed_generation(tmp_path):
    s = _settings(tmp_path)
    store = KbGenerationStore(s)
    store.bootstrap()  # gen 1

    gen2 = store.allocate()
    assert gen2.id == 2
    assert gen2.collection_name == f"{s.vector_collection}__g2"
    assert gen2.bm25_path == str(s.data_dir / "bm25s_index" / "g2")
    assert gen2.status == "building"


def test_activate_switches_active_generation(tmp_path):
    s = _settings(tmp_path)
    store = KbGenerationStore(s)
    store.bootstrap()
    assert store.current().id == 1

    gen2 = store.allocate()
    store.activate(gen2.id)
    assert store.current().id == 2
    assert store.current().status == "active"


def test_mark_failed_leaves_active_unchanged(tmp_path):
    s = _settings(tmp_path)
    store = KbGenerationStore(s)
    store.bootstrap()

    gen2 = store.allocate()
    store.mark_failed(gen2.id)
    assert store.current().id == 1  # gen 1 still active


def test_rebuild_writes_generation_2_without_deleting_generation_1(tmp_path):
    """A rebuild creates a new collection; the old one survives."""
    s = _settings(tmp_path)
    (tmp_path / "kb").mkdir(exist_ok=True)
    (tmp_path / "kb" / "doc.md").write_text("# Doc\ncontent here", encoding="utf-8")

    store = LocalVectorStore(s)
    stats1 = store.build(reset=True)
    assert stats1.chunks >= 1

    # Gen 1 active, collection has data
    gen = store._gen_store.current()
    assert gen.id == 1
    assert store._repo.count(gen.collection_name) >= 1

    # Allocate gen 2 and build into it
    gen2 = store._gen_store.allocate()
    stats2 = store.build(
        reset=True,
        target_collection=gen2.collection_name,
        target_bm25_path=Path(gen2.bm25_path),
    )
    assert stats2.chunks >= 1
    assert store._repo.count(gen2.collection_name) >= 1

    # Gen 1 collection still intact
    assert store._repo.count(gen.collection_name) >= 1


def test_failed_build_leaves_generation_1_active(tmp_path):
    """If build fails, gen 1 stays active and searchable."""
    s = _settings(tmp_path)
    (tmp_path / "kb").mkdir(exist_ok=True)
    (tmp_path / "kb" / "doc.md").write_text("# Doc\ncontent", encoding="utf-8")

    store = LocalVectorStore(s)
    store.build(reset=True)

    # Simulate a failed gen 2
    gen2 = store._gen_store.allocate()
    store._gen_store.mark_failed(gen2.id)

    # Gen 1 still active and searchable
    assert store._gen_store.current().id == 1
    results = store.search("content", top_k=2)
    assert results  # search still works on gen 1


def test_activation_switches_bm25_cache(tmp_path):
    """After activation, search uses the new generation's BM25."""
    s = _settings(tmp_path)
    (tmp_path / "kb").mkdir(exist_ok=True)
    (tmp_path / "kb" / "doc.md").write_text("# Doc\nunique keyword", encoding="utf-8")

    store = LocalVectorStore(s)
    store.build(reset=True)

    # BM25 cache has gen 1 key
    gen1 = store._gen_store.current()
    assert gen1.collection_name in Bm25Registry._cache

    # Allocate and build gen 2
    gen2 = store._gen_store.allocate()
    store.build(
        reset=True,
        target_collection=gen2.collection_name,
        target_bm25_path=Path(gen2.bm25_path),
    )
    store._gen_store.activate(gen2.id)

    # Active BM25 resolves to gen 2
    bm = store._active_bm25()
    assert bm is not None
    assert gen2.collection_name in Bm25Registry._cache


# ── helpers ──


def _settings(tmp_path) -> Settings:
    return Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        embedding_dimensions=128,
        embedding_provider="local",
    )
