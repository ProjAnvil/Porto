from __future__ import annotations

from fastapi.testclient import TestClient

from porto_chatbot.main import app


def test_list_facts_empty(tmp_path, monkeypatch):
    from porto_chatbot.api.routes import sessions as sess_mod
    from porto_chatbot.memory.session_store import SessionStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    SessionStore(settings)  # 初始化 SQLite（含 session_facts 表）
    monkeypatch.setattr(sess_mod, "current_settings", lambda: settings)

    client = TestClient(app)
    resp = client.get("/api/sessions/s1/facts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "s1"
    assert body["facts"] == []


def test_list_facts_returns_active(tmp_path, monkeypatch):
    from porto_chatbot.api.routes import sessions as sess_mod
    from porto_chatbot.memory.facts import SessionFactsStore
    from porto_chatbot.memory.session_store import SessionStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    SessionStore(settings)  # 初始化 SQLite（含 session_facts 表）
    SessionFactsStore(settings).upsert(
        session_id="s1", category="user_decision",
        content="登录采用 OAuth", source_msg_id="m1",
    )
    monkeypatch.setattr(sess_mod, "current_settings", lambda: settings)

    client = TestClient(app)
    resp = client.get("/api/sessions/s1/facts")
    body = resp.json()
    assert len(body["facts"]) == 1
    assert body["facts"][0]["content"] == "登录采用 OAuth"
    assert body["facts"][0]["category"] == "user_decision"
