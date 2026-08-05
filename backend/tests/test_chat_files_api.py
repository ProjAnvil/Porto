# backend/tests/test_chat_files_api.py
"""Tests for Task 11: POST /api/chat/files upload endpoint + ChatRequest.file_ids.

Covers:
- Upload endpoint stores file via FileService and returns file_id/page_count/original_name
- Sync def handler runs in threadpool (sync callable introspection)
- Error paths: missing extension, unsupported extension, oversize file
- ChatRequest.file_ids defaults to empty list and accepts list[str]
- _chat_request_from_stream_body forwards file_ids from both body shapes
"""
from __future__ import annotations

import inspect
import io

from fastapi.testclient import TestClient

from porto_chatbot.api.sse import _chat_request_from_stream_body
from porto_chatbot.models import ChatRequest


def _setup_client(monkeypatch, tmp_path):
    """Build a TestClient with an isolated data_dir for FileService storage."""
    from porto_chatbot import main
    from porto_chatbot.settings import Settings

    settings = Settings(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        embedding_provider="local",
        embedding_dimensions=128,
    )
    monkeypatch.setattr(main, "settings", settings)
    return TestClient(main.app)


class TestUploadChatFileEndpoint:
    def test_upload_txt_returns_file_id_and_metadata(self, monkeypatch, tmp_path):
        client = _setup_client(monkeypatch, tmp_path)
        body = "# PRD\n\nPayment service handles auth and refund.\n"
        resp = client.post(
            "/api/chat/files",
            files={"file": ("prd.txt", io.BytesIO(body.encode("utf-8")), "text/plain")},
            data={"session_id": "sess-1"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        # Contract fields per the brief
        assert set(payload) >= {"file_id", "page_count", "original_name"}
        assert isinstance(payload["file_id"], str)
        assert payload["file_id"]  # non-empty
        assert payload["original_name"] == "prd.txt"
        assert payload["page_count"] >= 1

    def test_upload_uses_default_session_id_when_omitted(self, monkeypatch, tmp_path):
        client = _setup_client(monkeypatch, tmp_path)
        resp = client.post(
            "/api/chat/files",
            files={"file": ("note.txt", io.BytesIO(b"hello world"), "text/plain")},
        )
        assert resp.status_code == 200
        assert resp.json()["file_id"]

    def test_upload_rejects_missing_extension(self, monkeypatch, tmp_path):
        client = _setup_client(monkeypatch, tmp_path)
        resp = client.post(
            "/api/chat/files",
            files={"file": ("noext", io.BytesIO(b"content"), "application/octet-stream")},
        )
        assert resp.status_code == 400

    def test_upload_rejects_unsupported_extension(self, monkeypatch, tmp_path):
        client = _setup_client(monkeypatch, tmp_path)
        resp = client.post(
            "/api/chat/files",
            files={"file": ("image.png", io.BytesIO(b"\x89PNG"), "image/png")},
        )
        assert resp.status_code == 415

    def test_upload_rejects_oversize_file(self, monkeypatch, tmp_path):
        client = _setup_client(monkeypatch, tmp_path)
        # Override the upload cap to a tiny value so we can exceed it without
        # allocating a 20 MB blob in the test process.
        from porto_chatbot import main
        from porto_chatbot.settings import Settings

        settings = Settings(
            data_dir=tmp_path / "data",
            log_dir=tmp_path / "logs",
            embedding_provider="local",
            embedding_dimensions=128,
            document_max_upload_mb=1,  # 1 MB cap; we will upload ~2 MB
        )
        monkeypatch.setattr(main, "settings", settings)
        client = TestClient(main.app)
        big = b"x" * (2 * 1024 * 1024)
        resp = client.post(
            "/api/chat/files",
            files={"file": ("big.txt", io.BytesIO(big), "text/plain")},
        )
        assert resp.status_code == 413

    def test_upload_returns_same_file_id_for_storage_via_get_info(
        self, monkeypatch, tmp_path
    ):
        """Round-trip: upload, then FileService.get_info sees the same file."""
        from porto_chatbot.api.deps import get_file_service

        client = _setup_client(monkeypatch, tmp_path)
        resp = client.post(
            "/api/chat/files",
            files={"file": ("doc.md", io.BytesIO(b"# Title\nbody"), "text/markdown")},
            data={"session_id": "sess-rt"},
        )
        assert resp.status_code == 200
        file_id = resp.json()["file_id"]

        info = get_file_service().get_info(file_id)
        assert info is not None
        assert info.original_name == "doc.md"
        assert info.file_id == file_id


class TestUploadHandlerIsSync:
    def test_upload_chat_file_is_sync_def(self):
        """M7 audit: the handler MUST be a sync ``def`` so FastAPI runs it in a
        threadpool, keeping the event loop free during FileService.store I/O.
        """
        from porto_chatbot.api.routes.chat import upload_chat_file

        assert not inspect.iscoroutinefunction(upload_chat_file), (
            "upload_chat_file must be a sync def (threadpool), not async"
        )


class TestChatRequestFileIds:
    def test_file_ids_defaults_to_empty_list(self):
        req = ChatRequest(message="hi")
        assert req.file_ids == []

    def test_file_ids_accepts_list_of_strings(self):
        req = ChatRequest(message="hi", file_ids=["a", "b", "c"])
        assert req.file_ids == ["a", "b", "c"]

    def test_file_ids_default_factory_creates_distinct_lists(self):
        """default_factory must yield a fresh list per instance (no shared mutable)."""
        a = ChatRequest(message="hi")
        b = ChatRequest(message="hi")
        a.file_ids.append("x")
        assert b.file_ids == []


class TestStreamBodyFileIdsParsing:
    def test_message_branch_forwards_file_ids(self):
        """When 'message' is present at top level, model_validate picks up file_ids."""
        req = _chat_request_from_stream_body(
            {"message": "hello", "file_ids": ["f1", "f2"]}
        )
        assert req.file_ids == ["f1", "f2"]
        assert req.message == "hello"

    def test_messages_branch_forwards_file_ids(self):
        """AI SDK stream body (messages array) — file_ids read from top level."""
        req = _chat_request_from_stream_body(
            {
                "id": "t1",
                "session_id": "s1",
                "file_ids": ["fa", "fb"],
                "messages": [
                    {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "hi"}]}
                ],
            }
        )
        assert req.file_ids == ["fa", "fb"]
        assert req.message == "hi"
        assert req.session_id == "s1"

    def test_messages_branch_file_ids_defaults_empty_when_absent(self):
        req = _chat_request_from_stream_body(
            {
                "messages": [
                    {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "hi"}]}
                ]
            }
        )
        assert req.file_ids == []

    def test_messages_branch_file_ids_none_becomes_empty(self):
        """Defensive: ``file_ids: null`` in body must not crash (becomes [])."""
        req = _chat_request_from_stream_body(
            {
                "file_ids": None,
                "messages": [
                    {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "hi"}]}
                ],
            }
        )
        assert req.file_ids == []
