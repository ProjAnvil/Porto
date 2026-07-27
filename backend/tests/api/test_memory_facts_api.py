from __future__ import annotations

from fastapi.testclient import TestClient

from porto_chatbot.main import app


def test_list_facts_empty(tmp_path, monkeypatch):
    from porto_chatbot.api.routes import memory as mem_mod
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    MemoryStore(settings)
    monkeypatch.setattr(mem_mod, "current_settings", lambda: settings)

    client = TestClient(app)
    resp = client.get("/api/memory/s1/facts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "s1"
    assert body["facts"] == []


def test_list_facts_returns_active(tmp_path, monkeypatch):
    from porto_chatbot.api.routes import memory as mem_mod
    from porto_chatbot.memory.facts import SessionFactsStore
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    MemoryStore(settings)
    SessionFactsStore(settings).upsert(
        session_id="s1", category="user_decision",
        content="登录采用 OAuth", source_msg_id="m1",
    )
    monkeypatch.setattr(mem_mod, "current_settings", lambda: settings)

    client = TestClient(app)
    resp = client.get("/api/memory/s1/facts")
    body = resp.json()
    assert len(body["facts"]) == 1
    assert body["facts"][0]["content"] == "登录采用 OAuth"
    assert body["facts"][0]["category"] == "user_decision"
