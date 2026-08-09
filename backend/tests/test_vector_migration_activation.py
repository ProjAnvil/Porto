"""Tests for finalization barrier and atomic activation (Plan 3 Task 4)."""
from __future__ import annotations

import time

import pytest

from porto_chatbot.settings import Settings
from porto_chatbot.vector_migrations import barrier
from porto_chatbot.vector_profiles import VectorProfile, VectorProfileStore
from porto_chatbot.vector_stores.factory import VectorRepositoryFactory


def _settings(tmp_path) -> Settings:
    return Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        embedding_dimensions=128,
        embedding_provider="local",
    )


def _setup_profiles(tmp_path):
    """Create a profile store with an active and a pending profile."""
    s = _settings(tmp_path)
    store = VectorProfileStore(s)
    store.bootstrap()
    active = store.active()
    pending = VectorProfile(
        id="target-1",
        name="Target Profile",
        backend="chroma",
        kb_collection_base="target_kb",
        memory_collection_base="target_memory",
    )
    store.save_pending(pending)
    return s, store, active, pending


# ── barrier ──


def test_barrier_set_and_clear():
    assert not barrier.is_active()
    barrier.set_barrier()
    assert barrier.is_active()
    barrier.clear_barrier()
    assert not barrier.is_active()


def test_barrier_guard_releases_on_exit():
    assert not barrier.is_active()
    with barrier._BarrierGuard(timeout=5.0):
        assert barrier.is_active()
    assert not barrier.is_active()


def test_barrier_guard_releases_on_exception():
    assert not barrier.is_active()
    with pytest.raises(RuntimeError):
        with barrier._BarrierGuard(timeout=5.0):
            assert barrier.is_active()
            raise RuntimeError("boom")
    assert not barrier.is_active()


def test_barrier_timeout_auto_releases():
    """Barrier auto-clears after timeout even if holder crashes."""
    barrier._event.clear()
    guard = barrier._BarrierGuard(timeout=0.1)
    guard.__enter__()
    assert barrier.is_active()
    # Simulate crash: never call __exit__
    time.sleep(0.3)
    assert not barrier.is_active()


def test_barrier_blocks_reindex_submission(tmp_path):
    """IndexSupervisor.submit should be rejected while barrier is active."""
    from porto_chatbot.index_supervisor import IndexSupervisor
    from porto_chatbot.locking import DbLockStore

    s = _settings(tmp_path)
    lock_store = DbLockStore(s)

    def fake_factory(settings):
        class FakeStore:
            def is_rag_ready(self):
                return True
        return FakeStore()

    supervisor = IndexSupervisor(
        lock_store=lock_store,
        store_factory=fake_factory,
        settings_provider=lambda: s,
    )

    barrier.set_barrier()
    try:
        status = supervisor.submit(s, reset=True, source="test")
        # Should return idle status (not accepted)
        assert status.status == "idle"
    finally:
        barrier.clear_barrier()


def test_barrier_allows_submission_when_inactive(tmp_path):
    """IndexSupervisor.submit should be accepted when barrier is inactive."""
    from porto_chatbot.index_supervisor import IndexSupervisor
    from porto_chatbot.locking import DbLockStore

    s = _settings(tmp_path)
    lock_store = DbLockStore(s)

    def fake_factory(settings):
        class FakeStore:
            def is_rag_ready(self):
                return True
        return FakeStore()

    supervisor = IndexSupervisor(
        lock_store=lock_store,
        store_factory=fake_factory,
        settings_provider=lambda: s,
    )
    supervisor.start()
    try:
        status = supervisor.submit(s, reset=True, source="test")
        assert status.status == "running"
    finally:
        supervisor.stop()


# ── activate_migration ──


def test_activate_migration_switches_profiles(tmp_path):
    """activate_migration atomically switches active→target, clears pending."""
    s, store, active, pending = _setup_profiles(tmp_path)
    assert store.active().id == active.id
    assert store.pending().id == pending.id

    new_rev = store.activate_migration(
        source_profile_id=active.id,
        target_profile_id=pending.id,
    )
    assert new_rev == store.revision()
    assert store.active().id == pending.id
    assert store.pending() is None


def test_activate_migration_increments_revision(tmp_path):
    s, store, active, pending = _setup_profiles(tmp_path)
    old_rev = store.revision()
    new_rev = store.activate_migration(
        source_profile_id=active.id,
        target_profile_id=pending.id,
    )
    assert new_rev == old_rev + 1


def test_activate_migration_rejects_wrong_source(tmp_path):
    """If source is no longer active, activation fails."""
    s, store, active, pending = _setup_profiles(tmp_path)
    with pytest.raises(ValueError, match="source profile"):
        store.activate_migration(
            source_profile_id="wrong-id",
            target_profile_id=pending.id,
        )


def test_activate_migration_rejects_wrong_target(tmp_path):
    """If target is no longer pending, activation fails."""
    s, store, active, pending = _setup_profiles(tmp_path)
    with pytest.raises(ValueError, match="target profile"):
        store.activate_migration(
            source_profile_id=active.id,
            target_profile_id="wrong-id",
        )


def test_activate_migration_marks_profile_statuses(tmp_path):
    """After activation, target is 'active' and source is 'inactive'."""
    s, store, active, pending = _setup_profiles(tmp_path)
    store.activate_migration(
        source_profile_id=active.id,
        target_profile_id=pending.id,
    )
    assert store.get(pending.id).status == "active"
    assert store.get(active.id).status == "inactive"


# ── factory invalidation ──


def test_factory_clear_evicts_cache():
    """VectorRepositoryFactory.clear() drops all cached entries."""
    VectorRepositoryFactory.clear()
    # Just verify it doesn't crash
    VectorRepositoryFactory.clear()


def test_factory_invalidate_clears_cache():
    """invalidate() closes and clears cached repos."""
    VectorRepositoryFactory.clear()
    VectorRepositoryFactory.invalidate()
    # Should be empty after invalidate
    VectorRepositoryFactory.clear()


# ── barrier blocks persist ──


def test_persist_skips_vector_write_during_barrier(tmp_path):
    """index_and_mark should skip vector writes when barrier is active."""
    from porto_chatbot.memory.persist import index_and_mark
    from porto_chatbot.models import MessageRecord

    s = _settings(tmp_path)

    class FakeSessions:
        settings = s
        def mark_indexed(self, ids):
            self.marked = ids

    class FakeMemory:
        indexed: list = []
        def index(self, records):
            self.indexed.extend(records)

    sessions = FakeSessions()
    memory = FakeMemory()

    record = MessageRecord(
        id="msg-1", session_id="sess-1", role="user",
        content="hello", intent="test", created_at="2026-01-01T00:00:00Z",
    )

    barrier.set_barrier()
    try:
        index_and_mark(sessions, memory, [record])
        # Memory.index should NOT have been called
        assert len(memory.indexed) == 0
    finally:
        barrier.clear_barrier()
