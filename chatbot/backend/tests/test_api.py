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


def test_chat_stream_native_streaming_when_llm_enabled(monkeypatch, sample_settings):
    """LLM enabled + agent_stream_enabled 时走原生 token 流（多个 text-delta 分片）。"""
    from porto_chatbot.llm import LLMClient

    sample_settings.agent_api_key = "k"
    sample_settings.agent_stream_enabled = True
    monkeypatch.setattr(main, "settings", sample_settings)
    # intent router 固定返回 rag；stream 返回多个分片
    monkeypatch.setattr(LLMClient, "complete_structured", lambda self, *a, **k: {"intent": "rag", "reason": "领域问题"})
    monkeypatch.setattr(LLMClient, "stream", lambda self, *a, **k: iter(["流", "式", "回答"]))

    client = TestClient(main.app)
    client.post("/api/kb/index")
    with client.stream(
        "POST",
        "/api/chat/stream",
        json={
            "id": "t1",
            "session_id": "s1",
            "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": "支付架构怎么拆"}]}],
        },
    ) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())

    assert "流" in text and "式" in text and "回答" in text
    assert text.count('"text-delta"') >= 3  # 原生流式：至少 3 个分片
    assert "data: [DONE]" in text
