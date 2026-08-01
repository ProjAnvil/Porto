# backend/tests/test_chat_dispatch.py
"""Verify chat endpoint dispatches to the correct backend via create_backend.

Task 5: chat() and chat_stream() route handlers become thin dispatchers that
call create_backend(settings, scope='chatbot'). These tests verify the dispatch
contract — the actual LangchainBackend logic is covered by test_chat_facts.py
and test_api.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


def test_chat_dispatches_via_create_backend_with_chatbot_scope(monkeypatch, tmp_path):
    """POST /api/chat must call create_backend(..., scope='chatbot') and return its answer."""
    from porto_chatbot import main
    from porto_chatbot.models import ChatResponse
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(main, "settings", settings)

    fake_response = ChatResponse(answer="dispatched", sources=[], steps=[])

    with patch("porto_chatbot.agent.factory.create_backend") as mock_factory:
        mock_backend = MagicMock()
        mock_backend.chat = AsyncMock(return_value=fake_response)
        mock_factory.return_value = mock_backend

        client = TestClient(main.app)
        resp = client.post("/api/chat", json={"message": "hi", "session_id": "t1"})

    assert resp.status_code == 200
    assert resp.json()["answer"] == "dispatched"
    mock_factory.assert_called_once()
    _, kwargs = mock_factory.call_args
    assert kwargs.get("scope") == "chatbot"


def test_chat_stream_dispatches_via_create_backend_with_chatbot_scope(monkeypatch, tmp_path):
    """POST /api/chat/stream must route through create_backend(scope='chatbot').chat_stream."""
    from porto_chatbot import main
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(main, "settings", settings)

    yielded = ["data: ping\n\n", "data: [DONE]\n\n"]

    async def _fake_stream(req, settings):
        for chunk in yielded:
            yield chunk

    with patch("porto_chatbot.agent.factory.create_backend") as mock_factory:
        mock_backend = MagicMock()
        mock_backend.chat_stream = _fake_stream
        mock_factory.return_value = mock_backend

        client = TestClient(main.app)
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "id": "t1",
                "session_id": "s1",
                "messages": [
                    {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "hi"}]}
                ],
            },
        ) as response:
            assert response.status_code == 200
            text = "".join(response.iter_text())

    assert "ping" in text
    assert "[DONE]" in text
    mock_factory.assert_called_once()
    _, kwargs = mock_factory.call_args
    assert kwargs.get("scope") == "chatbot"
