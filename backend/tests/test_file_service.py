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


def _build_text_pdf(pages_text: list[list[str]]) -> bytes:
    """Hand-craft a minimal valid PDF whose pages contain real extractable text.

    Each entry of ``pages_text`` becomes one page; lines are drawn top-to-bottom
    using the Helvetica core font so pypdf's ``extract_text`` returns them. This
    avoids pulling in reportlab/fpdf as a test-only dependency while still
    exercising the real ``FileService._extract_pdf_pages`` code path.
    """
    objects: list[bytes] = []

    def add(obj_bytes: bytes) -> int:
        objects.append(obj_bytes)
        return len(objects)  # 1-based obj id

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    pages_obj_id = 2
    objects.append(b"")  # slot 2 reserved; filled in after kids known
    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    for lines in pages_text:
        body = ["BT", "/F1 18 Tf", "72 720 Td", "18 TL"]
        for idx, line in enumerate(lines):
            safe = (
                line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            )
            if idx == 0:
                body.append(f"({safe}) Tj")
            else:
                body.extend(["T*", f"({safe}) Tj"])
        body.append("ET")
        stream = "\n".join(body).encode("latin-1")
        content_id = add(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream"
        )
        page_ids.append(
            add(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Contents " + str(content_id).encode() + b" 0 R "
                b"/Resources << /Font << /F1 " + str(font_id).encode() + b" 0 R >> >> >>"
            )
        )

    kids = b" ".join(f"{pid} 0 R".encode() for pid in page_ids)
    objects[pages_obj_id - 1] = (
        b"<< /Type /Pages /Kids [" + kids + b"] /Count "
        + str(len(page_ids)).encode() + b" >>"
    )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(n).encode() + b" /Root 1 0 R >>\nstartxref\n"
    out += str(xref_pos).encode() + b"\n%%EOF"
    return bytes(out)


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


class TestFileServiceReadPages:
    def test_read_pages_full_txt(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        # Two virtual pages (each >2000 chars so they split into distinct pages)
        page1 = "alpha " * 500  # ~3000 chars
        page2 = "beta " * 500
        body = page1 + page2
        meta = service.store(_make_upload("doc.txt", body.encode("utf-8")), "u1")

        text = service.read_pages(meta.file_id, 1, meta.page_count)
        assert text.startswith("--- 第 1 页 ---")
        assert "alpha" in text
        assert "beta" in text
        # Page separator header for the last page is present
        assert f"--- 第 {meta.page_count} 页 ---" in text

    def test_read_pages_single_page(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        meta = service.store(
            _make_upload("small.txt", b"hello world"), "u1"
        )
        assert meta.page_count == 1
        text = service.read_pages(meta.file_id, 1, 1)
        assert text == "--- 第 1 页 ---\nhello world"

    def test_read_pages_missing_file_returns_error_string(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        result = service.read_pages("nope", 1, 1)
        assert result == "[错误] 文件 nope 不存在"
        # Must not raise — error is a structured string
        assert isinstance(result, str)

    def test_read_pages_out_of_range_returns_error_string(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        meta = service.store(_make_upload("doc.txt", b"abc"), "u1")
        assert meta.page_count == 1

        too_high = service.read_pages(meta.file_id, 1, 5)
        assert too_high == f"[错误] 页码范围无效，文件共 {meta.page_count} 页"

        reversed_range = service.read_pages(meta.file_id, 2, 1)
        assert reversed_range == f"[错误] 页码范围无效，文件共 {meta.page_count} 页"

        zero_start = service.read_pages(meta.file_id, 0, 1)
        assert zero_start == f"[错误] 页码范围无效，文件共 {meta.page_count} 页"

    def test_read_pages_real_pdf_extracts_text(self, tmp_path):
        """PDF fixture: store a 3-page text PDF and read pages back."""
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        pdf_bytes = _build_text_pdf(
            [
                ["Introduction to Porto", "Payment module handles auth."],
                ["Second page topic", "Search target: UNIQUE_TOKEN_42"],
                ["Tail page", "Final remarks"],
            ]
        )
        meta = service.store(_make_upload("real.pdf", pdf_bytes), "u1")

        # _extract_pdf_pages ran during store and produced 3 real pages
        assert meta.page_count == 3

        # Read the full document
        full = service.read_pages(meta.file_id, 1, 3)
        assert "--- 第 1 页 ---" in full
        assert "--- 第 2 页 ---" in full
        assert "--- 第 3 页 ---" in full
        assert "UNIQUE_TOKEN_42" in full
        assert "Introduction to Porto" in full


class TestFileServiceSearch:
    def test_search_returns_hits_with_snippet(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        # Build a multi-page txt where the query lives on page 2
        page1 = "A" * 2500
        page2_prefix = "x" * 100
        page2_infix = "FindMe secret token"
        page2_suffix = "y" * 100
        page2 = page2_prefix + page2_infix + page2_suffix
        body = page1 + page2
        meta = service.store(_make_upload("haystack.txt", body.encode("utf-8")), "u1")

        hits = service.search(meta.file_id, "findme")
        assert len(hits) == 1
        hit = hits[0]
        assert hit.page == 2
        # Snippet is centred on the match, ±60 chars, so prefix/suffix trimmed
        assert "FindMe" in hit.snippet
        assert len(hit.snippet) <= len(page2_infix) + 2 * 60

    def test_search_is_case_insensitive(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        meta = service.store(
            _make_upload("mixed.txt", b"Hello WORLD xyz"), "u1"
        )
        hits_lower = service.search(meta.file_id, "world")
        hits_upper = service.search(meta.file_id, "WORLD")
        assert {h.page for h in hits_lower} == {1}
        assert {h.page for h in hits_upper} == {1}

    def test_search_multiple_occurrences_across_pages(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        # Two virtual pages, each contains the token
        body = ("target token here " * 200) + "padding " + ("target token again " * 200)
        meta = service.store(_make_upload("multi.txt", body.encode("utf-8")), "u1")
        hits = service.search(meta.file_id, "target")
        assert len(hits) >= 2
        # At least one hit per page where the token appears
        pages_hit = {h.page for h in hits}
        assert pages_hit.issubset(set(range(1, meta.page_count + 1)))

    def test_search_no_match_returns_empty_list(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        meta = service.store(_make_upload("doc.txt", b"nothing here"), "u1")
        assert service.search(meta.file_id, "absenttoken") == []

    def test_search_missing_file_returns_empty_list(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        # Unknown file_id must not raise
        assert service.search("missing", "foo") == []

    def test_search_empty_query_returns_empty_list(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        meta = service.store(_make_upload("doc.txt", b"some text"), "u1")
        assert service.search(meta.file_id, "") == []

    def test_search_real_pdf_finds_token(self, tmp_path):
        """PDF fixture: search across real extracted PDF pages."""
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        pdf_bytes = _build_text_pdf(
            [
                ["alpha page content"],
                ["beta page content with SPECIAL_FINDME_WORD"],
                ["gamma page content"],
            ]
        )
        meta = service.store(_make_upload("real.pdf", pdf_bytes), "u1")

        hits = service.search(meta.file_id, "special_findme_word")
        assert len(hits) == 1
        assert hits[0].page == 2
        assert "SPECIAL_FINDME_WORD" in hits[0].snippet


class TestFileServiceGetInfo:
    def test_get_info_returns_fileinfo(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        body = b"hello world content"
        meta = service.store(_make_upload("note.txt", body), "u1")

        info = service.get_info(meta.file_id)
        assert info is not None
        assert isinstance(info, FileInfo)
        assert info.file_id == meta.file_id
        assert info.original_name == "note.txt"
        assert info.mime == "text/plain"
        assert info.size_bytes == len(body)
        assert info.page_count == meta.page_count

    def test_get_info_missing_returns_none(self, tmp_path):
        settings = _make_settings(tmp_path)
        service = FileService(settings)
        assert service.get_info("does-not-exist") is None



