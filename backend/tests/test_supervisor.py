"""Unit tests for IndexSupervisor / DbLockStore / HealthMonitor."""

from __future__ import annotations

import threading
import time

from porto_chatbot.health import HealthMonitor
from porto_chatbot.index_supervisor import IndexSupervisor
from porto_chatbot.locking import RAG_INDEX_LOCK, DbLockStore
from porto_chatbot.models import IndexStats
from porto_chatbot.vector_store import LocalVectorStore

# ---------------------------- DbLockStore state machine ---------------------------- #


def test_lock_initial_idle(sample_settings):
    lock = DbLockStore(sample_settings)
    status = lock.get_status()
    assert status.status == "idle"
    assert status.last_indexed_at is None


def test_lock_running_to_succeeded_persists_last_indexed(sample_settings):
    lock = DbLockStore(sample_settings)
    lock.mark_running(RAG_INDEX_LOCK, source="manual", reset=True, total=3)
    assert lock.get_status().status == "running"

    lock.update_progress(RAG_INDEX_LOCK, done=2, total=3, chunks_done=7)
    assert lock.get_status().progress_done == 2
    assert lock.get_status().chunks_done == 7

    stats = IndexStats(kb_path="x", documents=3, chunks=9, embedding_provider="local")
    lock.mark_succeeded(RAG_INDEX_LOCK, stats=stats)

    status = lock.get_status()
    assert status.status == "succeeded"
    assert status.progress_done == status.progress_total == 3
    assert status.last_indexed_at is not None
    assert status.last_stats is not None
    assert status.last_stats.chunks == 9


def test_lock_mark_failed_records_error(sample_settings):
    lock = DbLockStore(sample_settings)
    lock.mark_running(RAG_INDEX_LOCK, source="manual", reset=True, total=1)
    lock.mark_failed(RAG_INDEX_LOCK, error="boom")
    status = lock.get_status()
    assert status.status == "failed"
    assert status.error == "boom"


def test_lock_mark_interrupted_only_clears_running(sample_settings):
    lock = DbLockStore(sample_settings)
    # A succeeded run must not be cleaned up by interrupt
    stats = IndexStats(kb_path="x", documents=1, chunks=1, embedding_provider="local")
    lock.mark_running(RAG_INDEX_LOCK, source="manual", reset=True, total=1)
    lock.mark_succeeded(RAG_INDEX_LOCK, stats=stats)

    affected = lock.mark_interrupted(RAG_INDEX_LOCK, error="x")
    assert affected == 0  # not running, nothing cleaned up
    assert lock.get_status().status == "succeeded"

    # Only running is cleaned up
    lock.mark_running(RAG_INDEX_LOCK, source="manual", reset=True, total=1)
    affected = lock.mark_interrupted(RAG_INDEX_LOCK, error="crashed")
    assert affected == 1
    assert lock.get_status().status == "interrupted"


# ----------------------------- IndexSupervisor ----------------------------- #


def _wait_status(sup: IndexSupervisor, terminal=("succeeded", "failed"), timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = sup.get_status()
        if st.status in terminal:
            return st
        time.sleep(0.1)
    raise AssertionError(f"status did not reach {terminal} (last={sup.get_status().status})")


def test_supervisor_indexes_and_records_progress(sample_settings):
    lock = DbLockStore(sample_settings)
    sup = IndexSupervisor(
        lock_store=lock,
        store_factory=lambda s: LocalVectorStore(s),
        settings_provider=lambda: sample_settings,
    )
    sup.start()
    try:
        status = sup.submit(sample_settings, source="manual")
        assert status.status == "running"

        final = _wait_status(sup)
        assert final.status == "succeeded"
        assert final.last_indexed_at is not None
        assert final.last_stats is not None
        assert final.last_stats.documents == 1
        assert final.progress_done == final.progress_total == 1
    finally:
        sup.stop()


def test_supervisor_rejects_when_busy(sample_settings):
    release = threading.Event()

    class _BlockingStore:
        _repo = type("_R", (), {"count": staticmethod(lambda c: 0)})()

        def build(self, reset=True, progress_cb=None, **kwargs):
            release.wait(timeout=5)
            return IndexStats(kb_path="x", documents=0, chunks=0, embedding_provider="local")

        def is_rag_ready(self):
            return True

    lock = DbLockStore(sample_settings)
    sup = IndexSupervisor(
        lock_store=lock,
        store_factory=lambda s: _BlockingStore(),
        settings_provider=lambda: sample_settings,
    )
    sup.start()
    try:
        first = sup.submit(sample_settings, source="first")
        assert first.status == "running"
        # The worker blocks inside _BlockingStore.build and _current is not cleared → the second
        # submit is rejected
        second = sup.submit(sample_settings, source="second")
        assert second.status == "running"
        assert second.source == "first"  # still the first job

        release.set()
        assert _wait_status(sup).status == "succeeded"
    finally:
        release.set()
        sup.stop()


def test_supervisor_clears_stale_running_on_start(sample_settings):
    lock = DbLockStore(sample_settings)
    lock.mark_running(RAG_INDEX_LOCK, source="manual", reset=True, total=5)
    assert lock.get_status().status == "running"

    sup = IndexSupervisor(
        lock_store=lock,
        store_factory=lambda s: LocalVectorStore(s),
        settings_provider=lambda: sample_settings,
    )
    sup.start()  # start() cleans leftover running → interrupted (no rebuild)
    try:
        assert sup.get_status().status == "interrupted"
    finally:
        sup.stop()


def test_rag_available_reflects_index_state(sample_settings):
    lock = DbLockStore(sample_settings)
    sup = IndexSupervisor(
        lock_store=lock,
        store_factory=lambda s: LocalVectorStore(s),
        settings_provider=lambda: sample_settings,
    )
    sup.start()
    try:
        available, reason = sup.rag_available()
        assert not available
        assert reason == "index_unavailable"

        LocalVectorStore(sample_settings).build()
        available, reason = sup.rag_available()
        assert available
        assert reason is None
    finally:
        sup.stop()


def test_supervisor_stop_joins_worker(sample_settings):
    """F4: stop() must join the worker — the background reindex has to be finished by
    teardown, otherwise the worker keeps operating on the collection (reset rebuild) and
    races the next test's collection operations → chromadb TOCTOU NotFoundError
    (test_list_and_delete flaked ~20%).

    While the worker is stuck in build, stop() must block until the worker exits (not return
    at once).
    """
    release = threading.Event()

    class _BlockingStore:
        _repo = type("_R", (), {"count": staticmethod(lambda c: 0)})()

        def build(self, reset=True, progress_cb=None, **kwargs):
            release.wait(timeout=5)
            return IndexStats(kb_path="x", documents=0, chunks=0, embedding_provider="local")

        def is_rag_ready(self):
            return True

    lock = DbLockStore(sample_settings)
    sup = IndexSupervisor(
        lock_store=lock,
        store_factory=lambda s: _BlockingStore(),
        settings_provider=lambda: sample_settings,
    )
    sup.start()
    try:
        sup.submit(sample_settings, source="test")
        time.sleep(0.1)  # give the worker time to enter build
        stopper = threading.Thread(target=sup.stop, daemon=True)
        stopper.start()
        time.sleep(0.2)
        # The worker is still stuck in build (release not set) → stop must still be blocking if
        # it joins
        assert stopper.is_alive(), (
            "stop() 未阻塞等待 worker —— 后台 reindex 会在 stop 返回后继续操作 collection"
        )
        release.set()
        stopper.join(timeout=5)
        assert not stopper.is_alive(), "stop() 在 worker 完成后仍未返回"
    finally:
        release.set()


# ----------------------------- HealthMonitor ----------------------------- #


def test_health_local_embedding_ok_and_no_key_unknown(sample_settings):
    lock = DbLockStore(sample_settings)
    sup = IndexSupervisor(
        lock_store=lock,
        store_factory=lambda s: LocalVectorStore(s),
        settings_provider=lambda: sample_settings,
    )
    hm = HealthMonitor(
        settings_provider=lambda: sample_settings,
        rag_available=sup.rag_available,
        rag_status=sup.get_status,
    )
    snap = hm.probe_all()
    deps = {d.name: d for d in snap.dependencies}
    assert deps["embedding"].status == "ok"  # local is always ok
    assert deps["agent_llm"].status == "unknown"  # no api key

    # No index → rag_search unavailable
    feats = {f.name: f for f in snap.features}
    assert feats["rag_search"].available is False
    assert feats["rag_search"].reason == "index_unavailable"


def test_health_marks_rag_unavailable_while_reindexing(sample_settings):
    release = threading.Event()
    lock = DbLockStore(sample_settings)

    class _BlockingStore:
        _repo = type("_R", (), {"count": staticmethod(lambda c: 0)})()

        def build(self, reset=True, progress_cb=None, **kwargs):
            release.wait(timeout=5)
            return IndexStats(kb_path="x", documents=0, chunks=0, embedding_provider="local")

        def is_rag_ready(self):
            return True

    sup = IndexSupervisor(
        lock_store=lock,
        store_factory=lambda s: _BlockingStore(),
        settings_provider=lambda: sample_settings,
    )
    sup.start()
    hm = HealthMonitor(
        settings_provider=lambda: sample_settings,
        rag_available=sup.rag_available,
        rag_status=sup.get_status,
    )
    try:
        sup.submit(sample_settings, source="manual")
        # The worker is running
        snap = hm.probe_all()
        assert snap.rag_index.status == "running"
        feats = {f.name: f for f in snap.features}
        assert feats["rag_search"].available is False
        assert feats["rag_search"].reason == "reindexing"
    finally:
        release.set()
        sup.stop()
