"""Tests for the vector migration API."""
from __future__ import annotations

from fastapi.testclient import TestClient

from porto_chatbot import main
from porto_chatbot.vector_profiles import VectorProfile, VectorProfileStore


def _setup(sample_settings):
    """Bootstrap profiles and create a pending profile."""
    store = VectorProfileStore(sample_settings)
    store.bootstrap()
    active = store.active()
    pending = VectorProfile(
        id="pending-test",
        name="Pending Chroma",
        backend="chroma",
        kb_collection_base=active.kb_collection_base,
        memory_collection_base=active.memory_collection_base,
    )
    store.save_pending(pending)
    return store


def test_get_status_empty(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    _setup(sample_settings)
    with TestClient(main.app) as client:
        resp = client.get("/api/vector-databases/migrate")
    assert resp.status_code == 200
    assert resp.json()["status"] is None


def test_start_migration_without_pending(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    store = VectorProfileStore(sample_settings)
    store.bootstrap()
    with TestClient(main.app) as client:
        resp = client.post("/api/vector-databases/migrate")
    assert resp.status_code == 409


def test_start_and_check_status(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    _setup(sample_settings)
    with TestClient(main.app) as client:
        resp = client.post("/api/vector-databases/migrate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] is not None
        assert data["target_profile_id"] == "pending-test"

        # Status survives refresh
        resp2 = client.get("/api/vector-databases/migrate")
        assert resp2.status_code == 200


def test_cancel_migration(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    _setup(sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/vector-databases/migrate")
        resp = client.post("/api/vector-databases/migrate/cancel")
        assert resp.status_code == 200
