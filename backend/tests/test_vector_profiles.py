"""Tests for vector profile persistence and bootstrap."""
from __future__ import annotations

import pytest

from porto_chatbot.settings import Settings
from porto_chatbot.vector_profiles import VectorProfile, VectorProfileStore


def _settings(tmp_path) -> Settings:
    return Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        embedding_dimensions=128,
        embedding_provider="local",
    )


# ── bootstrap ──


def test_bootstrap_creates_active_chroma_profile(tmp_path):
    s = _settings(tmp_path)
    store = VectorProfileStore(s)
    profile = store.bootstrap()
    assert profile is not None
    assert profile.backend == "chroma"
    assert profile.metric == "l2"
    assert profile.status == "active"
    assert profile.kb_collection_base == s.vector_collection
    assert profile.memory_collection_base == s.memory_collection
    assert profile.uri is None
    assert profile.credential_configured is True  # chroma = no auth needed

    # Second bootstrap is a no-op
    assert store.bootstrap() is None


def test_revision_starts_at_1(tmp_path):
    s = _settings(tmp_path)
    store = VectorProfileStore(s)
    store.bootstrap()
    assert store.revision() == 1


def test_active_returns_bootstrapped_profile(tmp_path):
    s = _settings(tmp_path)
    store = VectorProfileStore(s)
    store.bootstrap()
    active = store.active()
    assert active is not None
    assert active.status == "active"


def test_pending_is_none_after_bootstrap(tmp_path):
    s = _settings(tmp_path)
    store = VectorProfileStore(s)
    store.bootstrap()
    assert store.pending() is None


# ── save pending ──


def test_save_pending_milvus_profile(tmp_path):
    s = _settings(tmp_path)
    store = VectorProfileStore(s)
    store.bootstrap()

    milvus = VectorProfile(
        id="milvus-1",
        name="Remote Milvus",
        backend="milvus",
        metric="cosine",
        uri="http://milvus:19530",
        database="porto",
        auth_mode="token",
        token="secret-token",
        kb_collection_base="porto_kb",
        memory_collection_base="porto_memory",
    )
    store.save_pending(milvus)

    pending = store.pending()
    assert pending is not None
    assert pending.id == "milvus-1"
    assert pending.backend == "milvus"
    assert pending.status == "pending"
    assert pending.credential_configured is True

    # Active is still Chroma
    assert store.active().backend == "chroma"


def test_save_pending_replaces_old_pending(tmp_path):
    s = _settings(tmp_path)
    store = VectorProfileStore(s)
    store.bootstrap()

    p1 = VectorProfile(
        id="p1", name="Milvus 1", backend="milvus",
        kb_collection_base="porto_kb", memory_collection_base="porto_memory",
    )
    store.save_pending(p1)
    assert store.pending().id == "p1"

    p2 = VectorProfile(
        id="p2", name="Milvus 2", backend="milvus",
        kb_collection_base="porto_kb", memory_collection_base="porto_memory",
    )
    store.save_pending(p2)
    assert store.pending().id == "p2"
    # Old pending profile is deleted
    assert store.get("p1") is None


# ── credential handling ──


def test_secret_omission_preserves_existing(tmp_path):
    """Updating without providing a credential keeps the old one."""
    s = _settings(tmp_path)
    store = VectorProfileStore(s)

    profile = VectorProfile(
        id="p1", name="Milvus", backend="milvus",
        auth_mode="token", token="original-secret",
        kb_collection_base="porto_kb", memory_collection_base="porto_memory",
    )
    store.save_pending(profile)

    # Update without token → original preserved
    store.update("p1", name="Renamed")
    fetched = store.get("p1")
    assert fetched.name == "Renamed"
    assert fetched.credential_configured is True


def test_clear_credential_removes_it(tmp_path):
    s = _settings(tmp_path)
    store = VectorProfileStore(s)

    profile = VectorProfile(
        id="p1", name="Milvus", backend="milvus",
        auth_mode="token", token="secret",
        kb_collection_base="porto_kb", memory_collection_base="porto_memory",
    )
    store.save_pending(profile)
    assert store.get("p1").credential_configured is True

    store.clear_credential("p1", "token")
    assert store.get("p1").credential_configured is False


# ── storage identity ──


def test_storage_identity_excludes_timeout_and_credentials(tmp_path):
    p1 = VectorProfile(
        id="p1", name="A", backend="milvus", uri="http://m:19530",
        database="db", tls=True, metric="cosine",
        auth_mode="token", token="secret-1",
        kb_collection_base="porto_kb", memory_collection_base="porto_memory",
    )
    p2 = VectorProfile(
        id="p2", name="B", backend="milvus", uri="http://m:19530",
        database="db", tls=True, metric="cosine",
        auth_mode="token", token="secret-2",  # different credential
        timeout=60,  # different timeout
        kb_collection_base="porto_kb", memory_collection_base="porto_memory",
    )
    assert p1.storage_identity() == p2.storage_identity()


def test_storage_identity_differs_on_backend_change(tmp_path):
    chroma = VectorProfile(
        id="c", name="Chroma", backend="chroma",
        kb_collection_base="porto_kb", memory_collection_base="porto_memory",
    )
    milvus = VectorProfile(
        id="m", name="Milvus", backend="milvus", uri="http://m:19530",
        kb_collection_base="porto_kb", memory_collection_base="porto_memory",
    )
    assert chroma.storage_identity() != milvus.storage_identity()


# ── delete inactive ──


def test_delete_inactive_profile(tmp_path):
    s = _settings(tmp_path)
    store = VectorProfileStore(s)
    store.bootstrap()

    pending = VectorProfile(
        id="p1", name="Pending", backend="milvus",
        kb_collection_base="porto_kb", memory_collection_base="porto_memory",
    )
    store.save_pending(pending)
    store.delete_inactive("p1")
    assert store.get("p1") is None
    assert store.pending() is None


def test_cannot_delete_active_profile(tmp_path):
    s = _settings(tmp_path)
    store = VectorProfileStore(s)
    active = store.bootstrap()

    with pytest.raises(ValueError, match="active"):
        store.delete_inactive(active.id)


# ── response dict has no secrets ──


def test_response_dict_excludes_credentials(tmp_path):
    profile = VectorProfile(
        id="p1", name="Milvus", backend="milvus",
        auth_mode="token", token="super-secret",
        username="user", password="pass",
        kb_collection_base="porto_kb", memory_collection_base="porto_memory",
    )
    d = profile.to_response_dict()
    assert "credential_configured" in d
    assert d["credential_configured"] is True
    assert "token" not in d
    assert "username" not in d
    assert "password" not in d
