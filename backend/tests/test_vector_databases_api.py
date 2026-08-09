"""Tests for the vector database profile API."""
from __future__ import annotations

from fastapi.testclient import TestClient

from porto_chatbot import main
from porto_chatbot.api.deps import current_settings
from porto_chatbot.vector_profiles import VectorProfileStore


def _bootstrap():
    """Ensure the profile store is bootstrapped for the current settings."""
    store = VectorProfileStore(current_settings())
    store.bootstrap()
    return store


# ── list ──


def test_list_profiles_returns_active_chroma(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    _bootstrap()
    with TestClient(main.app) as client:
        resp = client.get("/api/vector-databases")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] is not None
    assert data["active"]["backend"] == "chroma"
    assert data["active"]["status"] == "active"
    assert data["pending"] is None
    assert data["revision"] == 1


# ── save ──


def test_save_pending_milvus_profile(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    _bootstrap()
    with TestClient(main.app) as client:
        resp = client.post("/api/vector-databases", json={
            "name": "Remote Milvus",
            "backend": "milvus",
            "metric": "cosine",
            "uri": "http://milvus:19530",
            "database": "porto",
            "auth_mode": "token",
            "token": "secret-token",
            "kb_collection_base": "porto_kb",
            "memory_collection_base": "porto_memory",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["profile"]["backend"] == "milvus"
    assert data["profile"]["status"] == "pending"
    assert data["migration_required"] is True
    assert "token" not in data["profile"]
    assert data["profile"]["credential_configured"] is True


def test_save_same_identity_no_migration(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    store = _bootstrap()
    active = store.active()
    with TestClient(main.app) as client:
        resp = client.post("/api/vector-databases", json={
            "name": "Same Chroma",
            "backend": "chroma",
            "metric": "l2",
            "kb_collection_base": active.kb_collection_base,
            "memory_collection_base": active.memory_collection_base,
        })
    assert resp.status_code == 200
    assert resp.json()["migration_required"] is False


# ── delete ──


def test_delete_pending_profile(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    _bootstrap()
    with TestClient(main.app) as client:
        resp = client.post("/api/vector-databases", json={
            "name": "To Delete",
            "backend": "milvus",
            "uri": "http://m:19530",
            "kb_collection_base": "porto_kb",
            "memory_collection_base": "porto_memory",
        })
        profile_id = resp.json()["profile"]["id"]
        resp = client.delete(f"/api/vector-databases/{profile_id}")
        assert resp.status_code == 200
        resp = client.get("/api/vector-databases")
        assert resp.json()["pending"] is None


def test_cannot_delete_active_profile(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    store = _bootstrap()
    active = store.active()
    with TestClient(main.app) as client:
        resp = client.delete(f"/api/vector-databases/{active.id}")
        assert resp.status_code == 409


# ── test connection ──


def test_test_connection_chroma(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    _bootstrap()
    with TestClient(main.app) as client:
        resp = client.post("/api/vector-databases/test", json={
            "name": "Chroma Test",
            "backend": "chroma",
            "kb_collection_base": "porto_kb",
            "memory_collection_base": "porto_memory",
        })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ── credential safety ──


def test_credentials_never_in_response(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    _bootstrap()
    with TestClient(main.app) as client:
        resp = client.post("/api/vector-databases", json={
            "name": "Cred Test",
            "backend": "milvus",
            "auth_mode": "user_password",
            "username": "admin",
            "password": "super-secret-pass",
            "uri": "http://m:19530",
            "kb_collection_base": "porto_kb",
            "memory_collection_base": "porto_memory",
        })
    data = resp.json()
    assert "super-secret-pass" not in str(data)
    assert "admin" not in str(data)
    assert data["profile"]["credential_configured"] is True
