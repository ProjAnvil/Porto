from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from porto_chatbot import main


@pytest.fixture()
def client(monkeypatch, sample_settings):
    """Shared FastAPI TestClient, bound to the sample_settings isolated under tmp_path.

    Follows the existing pattern in test_api.py / test_workflow_api.py:
    ``monkeypatch.setattr(main, "settings", sample_settings)`` makes every code path that
    reads settings through ``current_settings()`` see the instance isolated under tmp_path.
    """
    monkeypatch.setattr(main, "settings", sample_settings)
    return TestClient(main.app)


def test_settings_include_rag_optimization(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rag_chat"]["intent_routing_mode"] == "binary"  # default value
    assert body["rag_chat"]["query_transform_strategy"] == "none"
    assert body["rag_workflow"]["query_transform_strategy"] == "none"


def test_settings_save_rag_chat(client):
    resp = client.put(
        "/api/settings",
        json={
            "rag_chat": {
                "intent_routing_mode": "adaptive",
                "query_transform_strategy": "hyde",
            }
        },
    )
    assert resp.status_code == 200
    assert resp.json()["rag_chat"]["intent_routing_mode"] == "adaptive"
    # Persistence
    assert client.get("/api/settings").json()["rag_chat"]["query_transform_strategy"] == "hyde"


def test_settings_save_rag_with_new_fields(client):
    resp = client.put(
        "/api/settings",
        json={
            "rag": {
                "embedding_provider": "openai_compatible",
                "embedding_api_key": "sk-test-123",
                "rerank_type": "cross_encoder",
                "rerank_base_url": "https://api.jina.ai/v1",
                "rerank_api_key": "jina-key",
            }
        },
    )
    assert resp.status_code == 200
    rag = resp.json()["rag"]
    assert rag["embedding_provider"] == "openai_compatible"
    assert rag["embedding_api_key"] == "sk-test-123"
    assert rag["rerank_type"] == "cross_encoder"
    assert rag["rerank_base_url"] == "https://api.jina.ai/v1"
    # Verify persistence
    persisted = client.get("/api/settings").json()["rag"]
    assert persisted["rerank_type"] == "cross_encoder"
