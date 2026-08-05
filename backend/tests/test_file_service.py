"""Tests for file metadata models: FileMeta, FileInfo, FileHit."""

from porto_chatbot.models.file import FileHit, FileInfo, FileMeta


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
