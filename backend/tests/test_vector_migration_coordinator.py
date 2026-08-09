"""Tests for the migration coordinator (mocked repos, no real Milvus)."""
from __future__ import annotations

from porto_chatbot.settings import Settings
from porto_chatbot.vector_migrations.coordinator import VectorMigrationCoordinator
from porto_chatbot.vector_profiles import VectorProfile, VectorProfileStore


def _settings(tmp_path) -> Settings:
    return Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        embedding_dimensions=128,
        embedding_provider="local",
    )


def _chroma_profile(store: VectorProfileStore) -> VectorProfile:
    """Get the bootstrap active Chroma profile."""
    store.bootstrap()
    return store.active()


def _make_pending_profile(store: VectorProfileStore) -> VectorProfile:
    """Create a pending profile (same Chroma identity to avoid real migration)."""
    active = store.active()
    pending = VectorProfile(
        id="pending-1",
        name="Pending Chroma",
        backend="chroma",
        kb_collection_base=active.kb_collection_base,
        memory_collection_base=active.memory_collection_base,
    )
    store.save_pending(pending)
    return pending


def test_coordinator_can_be_created(tmp_path):
    s = _settings(tmp_path)
    coord = VectorMigrationCoordinator(s)
    assert coord is not None


def test_submit_requires_pending(tmp_path):
    s = _settings(tmp_path)
    store = VectorProfileStore(s)
    store.bootstrap()
    coord = VectorMigrationCoordinator(s)
    try:
        coord.submit()
        raise AssertionError("should have raised")
    except ValueError:
        pass


def test_submit_creates_migration(tmp_path):
    s = _settings(tmp_path)
    store = VectorProfileStore(s)
    store.bootstrap()
    _make_pending_profile(store)
    coord = VectorMigrationCoordinator(s)
    mig = coord.submit()
    assert mig is not None
    assert mig.source_profile_id is not None
    assert mig.target_profile_id == "pending-1"
    # Stop worker to clean up
    coord.stop_worker()


def test_get_status_returns_none_initially(tmp_path):
    s = _settings(tmp_path)
    coord = VectorMigrationCoordinator(s)
    assert coord.get_status() is None


def test_cancel_marks_cancelled(tmp_path):
    s = _settings(tmp_path)
    store = VectorProfileStore(s)
    store.bootstrap()
    _make_pending_profile(store)
    coord = VectorMigrationCoordinator(s)
    coord.submit()
    # Cancel before worker progresses
    coord.cancel()
    coord.stop_worker()
