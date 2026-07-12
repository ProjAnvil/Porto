from __future__ import annotations

from fastapi.testclient import TestClient

from porto_chatbot import main


def test_api_chat_and_workflow(monkeypatch, sample_settings, sample_prd):
    monkeypatch.setattr(main, "settings", sample_settings)
    client = TestClient(main.app)

    index_resp = client.post("/api/kb/index")
    assert index_resp.status_code == 200
    assert index_resp.json()["documents"] == 1

    chat_resp = client.post("/api/chat", json={"message": "支付和风控怎么拆？"})
    assert chat_resp.status_code == 200
    assert "answer" in chat_resp.json()
    assert chat_resp.json()["sources"]

    wf_resp = client.post(
        "/api/porto/workflows",
        json={"text": sample_prd, "project_name": "支付平台"},
    )
    assert wf_resp.status_code == 200
    data = wf_resp.json()
    assert data["evaluation"]["passed"] is True
    assert any(s["name"] == "payment-service" for s in data["subsystems"])


def test_api_chat_stream_ai_sdk_format(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    client = TestClient(main.app)
    client.post("/api/kb/index")

    with client.stream(
        "POST",
        "/api/chat/stream",
        json={
            "id": "thread-1",
            "session_id": "test-session",
            "messages": [
                {
                    "id": "msg-1",
                    "role": "user",
                    "parts": [{"type": "text", "text": "支付和风控怎么拆？"}],
                }
            ],
        },
    ) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())

    assert 'data: {"type": "start"' in text
    assert 'data: {"type": "text-start"' in text
    assert 'data: {"type": "text-delta"' in text
    assert 'data: {"type": "source-document"' in text
    assert 'data: {"type": "data-porto"' in text
    assert 'data: {"type": "finish"' in text
    assert "data: [DONE]" in text


def test_api_chat_greeting_skips_rag(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    client = TestClient(main.app)

    chat_resp = client.post("/api/chat", json={"message": "你好"})

    assert chat_resp.status_code == 200
    data = chat_resp.json()
    assert data["sources"] == []
    assert data["memory"] == []
    assert data["steps"][0]["name"] == "route_intent"
    assert data["steps"][0]["data"]["intent"] == "direct"
    assert all(step["name"] != "retrieve_knowledge" for step in data["steps"])
