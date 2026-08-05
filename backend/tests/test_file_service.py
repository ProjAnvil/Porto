"""Tests for file metadata models: FileMeta, FileInfo, FileHit."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

from fastapi import UploadFile

from porto_chatbot.files.service import FileService, _split_virtual_pages
from porto_chatbot.models.file import FileHit, FileInfo, FileMeta
from porto_chatbot.settings import Settings


class TestFileMeta:
    def test_construct_with_all_fields(self):
        m = FileMeta(
            file_id="f1",
            owner_id="u1",
            original_name="doc.pdf",
            stored_path="/data/doc.pdf",
            mime="application/pdf",
            size_bytes=1024,
            page_count=5,
        )
        assert m.file_id == "f1"
        assert m.owner_id == "u1"
        assert m.original_name == "doc.pdf"
        assert m.stored_path == "/data/doc.pdf"
        assert m.mime == "application/pdf"
        assert m.size_bytes == 1024
        assert m.page_count == 5

    def test_created_at_auto_filled(self):
        m = FileMeta(
            file_id="f2",
            owner_id="u2",
            original_name="img.png",
            stored_path="/data/img.png",
            mime="image/png",
            size_bytes=2048,
            page_count=1,
        )
        assert m.created_at is not None
        # Should be a valid ISO-format string
        assert "T" in m.created_at

    def test_created_at_can_be_overridden(self):
        m = FileMeta(
            file_id="f3",
            owner_id="u3",
            original_name="note.txt",
            stored_path="/data/note.txt",
            mime="text/plain",
            size_bytes=100,
            page_count=1,
            created_at="2026-01-01T00:00:00+00:00",
        )
        assert m.created_at == "2026-01-01T00:00:00+00:00"


class TestFileInfo:
    def test_construct(self):
        info = FileInfo(
            file_id="f1",
            original_name="doc.pdf",
            mime="application/pdf",
            size_bytes=1024,
            page_count=5,
        )
        assert info.file_id == "f1"
        assert info.original_name == "doc.pdf"
        assert info.mime == "application/pdf"
        assert info.size_bytes == 1024
        assert info.page_count == 5


class TestFileHit:
    def test_construct(self):
        hit = FileHit(page=3, snippet="some text from page 3")
        assert hit.page == 3
        assert hit.snippet == "some text from page 3"


def _make_settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, log_dir=tmp_path / "logs")


def _make_upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content))


class TestSplitVirtualPages:
    def test_short_text_returns_single_page(self):
        assert _split_virtual_pages("hello world") == ["hello world"]

    def test_long_text_is_chunked(self):
        text = "x" * 4500  # crosses two 2000-char boundaries
        pages = _split_virtual_pages(text)
        assert len(pages) == 3
        assert all(len(p) <= 2000 for p in pages)
        assert "".join(pages) == text

    def test_empty_text_returns_single_empty_page(self):
        assert _split_virtual_pages("") == [""]


class TestFileServiceStore:
    def test_store_txt_persists_file_and_metadata(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        body = "# PRD\n\nPayment service handles auth and refund.\n" * 3

        meta = service.store(_make_upload("prd.txt", body.encode("utf-8")), "user-1")

        # Returned metadata is correct
        assert meta.owner_id == "user-1"
        assert meta.original_name == "prd.txt"
        assert meta.mime == "text/plain"
        assert meta.size_bytes == len(body.encode("utf-8"))
        assert meta.page_count >= 1

        # File is on disk at the recorded path
        stored = Path(meta.stored_path)
        assert stored.exists()
        assert stored.read_bytes() == body.encode("utf-8")

        # SQLite row matches metadata and embeds page text
        with sqlite3.connect(settings.files_db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM files WHERE file_id=?", (meta.file_id,)
            ).fetchone()
        assert row is not None
        assert row["owner_id"] == "user-1"
        assert row["original_name"] == "prd.txt"
        assert row["mime"] == "text/plain"
        assert row["size_bytes"] == meta.size_bytes
        assert row["page_count"] == meta.page_count

        pages = json.loads(row["pages_json"])
        assert isinstance(pages, list)
        assert len(pages) == meta.page_count
        assert "".join(pages).startswith("# PRD")

    def test_store_creates_files_dir_and_db(self, tmp_path):
        settings = _make_settings(tmp_path)
        # Directories do not exist yet
        assert not (tmp_path / "files").exists()
        assert not (tmp_path / "files.sqlite3").exists()

        FileService(settings)

        assert (tmp_path / "files").is_dir()
        assert (tmp_path / "files.sqlite3").is_file()

    def test_store_two_files_get_distinct_ids(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)

        m1 = service.store(_make_upload("a.txt", b"aaa"), "u")
        m2 = service.store(_make_upload("b.txt", b"bbb"), "u")

        assert m1.file_id != m2.file_id
        assert Path(m1.stored_path).parent != Path(m2.stored_path).parent


