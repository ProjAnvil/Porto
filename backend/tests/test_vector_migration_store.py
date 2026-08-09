"""Tests for vector migration state machine and locking."""
from __future__ import annotations

import pytest

from porto_chatbot.settings import Settings
from porto_chatbot.vector_migrations.store import (
    MigrationPhase,
    MigrationStatus,
    VectorMigrationStore,
)


def _settings(tmp_path) -> Settings:
    return Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        embedding_dimensions=128,
        embedding_provider="local",
    )


# ── create ──


def test_create_migration(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    mig = store.create(source_profile_id="src", target_profile_id="tgt")
    assert mig.status == MigrationStatus.RUNNING
    assert mig.phase == MigrationPhase.COPYING
    assert mig.source_profile_id == "src"


def test_only_one_running_migration(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    store.create(source_profile_id="a", target_profile_id="b")
    with pytest.raises(ValueError, match="already running"):
        store.create(source_profile_id="c", target_profile_id="d")


# ── transitions ──


def test_transition_phase(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    mig = store.create(source_profile_id="s", target_profile_id="t")
    store.transition(mig.id, phase=MigrationPhase.CATCHING_UP)
    updated = store.get(mig.id)
    assert updated.phase == MigrationPhase.CATCHING_UP


def test_transition_to_succeeded_sets_finished(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    mig = store.create(source_profile_id="s", target_profile_id="t")
    store.transition(mig.id, status=MigrationStatus.SUCCEEDED)
    updated = store.get(mig.id)
    assert updated.status == MigrationStatus.SUCCEEDED
    assert updated.finished_at is not None


def test_transition_to_failed_with_error(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    mig = store.create(source_profile_id="s", target_profile_id="t")
    store.transition(mig.id, status=MigrationStatus.FAILED, error="connection lost")
    updated = store.get(mig.id)
    assert updated.status == MigrationStatus.FAILED
    assert updated.error == "connection lost"


# ── retry after failure ──


def test_failed_can_retry(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    mig = store.create(source_profile_id="s", target_profile_id="t")
    store.transition(mig.id, status=MigrationStatus.FAILED, error="boom")
    # Should be able to create a new migration
    mig2 = store.create(source_profile_id="s", target_profile_id="t")
    assert mig2.id != mig.id


def test_cancelled_can_retry(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    mig = store.create(source_profile_id="s", target_profile_id="t")
    store.cancel(mig.id)
    mig2 = store.create(source_profile_id="s", target_profile_id="t")
    assert mig2.status == MigrationStatus.RUNNING


# ── cancel ──


def test_cancel_before_activating(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    mig = store.create(source_profile_id="s", target_profile_id="t")
    store.cancel(mig.id)
    assert store.get(mig.id).status == MigrationStatus.CANCELLED


def test_cannot_cancel_activating(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    mig = store.create(source_profile_id="s", target_profile_id="t")
    store.transition(mig.id, phase=MigrationPhase.ACTIVATING)
    with pytest.raises(ValueError, match="activating"):
        store.cancel(mig.id)


# ── stale heartbeat ──


def test_interrupt_stale_heartbeat(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    mig = store.create(source_profile_id="s", target_profile_id="t")
    # Manually set old heartbeat
    from datetime import UTC, datetime, timedelta

    old_hb = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    with store._conn() as conn:
        conn.execute(
            "UPDATE vector_migrations SET heartbeat_at=? WHERE id=?",
            (old_hb, mig.id),
        )
        conn.commit()
    count = store.interrupt_stale(max_age_seconds=90)
    assert count == 1
    assert store.get(mig.id).status == MigrationStatus.INTERRUPTED


# ── progress ──


def test_update_progress(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    mig = store.create(source_profile_id="s", target_profile_id="t")
    store.update_progress(
        mig.id, kb_copied=100, kb_total=500, memory_copied=50, memory_total=200,
    )
    updated = store.get(mig.id)
    assert updated.kb_copied == 100
    assert updated.kb_total == 500
    assert updated.memory_copied == 50


# ── current / latest ──


def test_current_returns_running(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    assert store.current() is None
    mig = store.create(source_profile_id="s", target_profile_id="t")
    assert store.current().id == mig.id


def test_current_or_latest_returns_terminal(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    mig = store.create(source_profile_id="s", target_profile_id="t")
    store.transition(mig.id, status=MigrationStatus.SUCCEEDED)
    assert store.current() is None
    latest = store.current_or_latest()
    assert latest is not None
    assert latest.status == MigrationStatus.SUCCEEDED


# ── verification ──


def test_save_verification(tmp_path):
    s = _settings(tmp_path)
    store = VectorMigrationStore(s)
    mig = store.create(source_profile_id="s", target_profile_id="t")
    store.save_verification(mig.id, '{"counts_match": true}')
    updated = store.get(mig.id)
    assert updated.verification_json == '{"counts_match": true}'
