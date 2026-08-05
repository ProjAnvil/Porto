"""FileService — store uploaded documents with pagination metadata.

Durable home for user-uploaded PRDs/reference files. Each upload is written
under ``settings.files_dir/<file_id>/<original_name>`` and a row is inserted
into ``settings.files_db_path`` (SQLite) recording mime/size/page breakdown.
Pages come from PDF text extraction or virtual 2000-char chunking for other
text-like formats so downstream nodes can cite ``page N`` consistently.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path

from fastapi import UploadFile

from ..documents import parse_document
from ..logging_utils import get_component_logger
from ..models.file import FileHit, FileInfo, FileMeta
from ..settings import Settings

_MIME_MAP = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".markdown": "text/markdown",
}
_VIRTUAL_PAGE_CHARS = 2000


def _split_virtual_pages(text: str, chars: int = _VIRTUAL_PAGE_CHARS) -> list[str]:
    return [text[i : i + chars] for i in range(0, len(text), chars)] or [""]


class FileService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("file_service", settings)
        self.settings.files_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.settings.files_db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS files (
                file_id TEXT PRIMARY KEY, owner_id TEXT, original_name TEXT,
                stored_path TEXT, mime TEXT, size_bytes INTEGER, page_count INTEGER,
                pages_json TEXT, created_at TEXT)"""
            )

    def store(self, file: UploadFile, owner_id: str) -> FileMeta:
        file_id = uuid.uuid4().hex[:16]
        original = file.filename or "unnamed"
        suffix = Path(original).suffix.lower()
        mime = _MIME_MAP.get(suffix, "application/octet-stream")
        payload = file.file.read()
        size = len(payload)
        store_dir = self.settings.files_dir / file_id
        store_dir.mkdir(parents=True, exist_ok=True)
        stored_path = store_dir / original
        stored_path.write_bytes(payload)
        max_bytes = getattr(self.settings, "document_max_upload_mb", 20) * 1024 * 1024
        max_pdf = getattr(self.settings, "document_max_pdf_pages", 200)
        try:
            artifact = parse_document(
                stored_path,
                original_name=original,
                max_bytes=max_bytes,
                max_pdf_pages=max_pdf,
            )
        except Exception:
            # T2-A: parse 失败时清理孤儿目录,避免 files_dir 堆积无 DB 记录的残留。
            shutil.rmtree(store_dir, ignore_errors=True)
            raise
        if suffix == ".pdf":
            pages = self._extract_pdf_pages(stored_path)
        else:
            pages = _split_virtual_pages(artifact.text)
        meta = FileMeta(
            file_id=file_id,
            owner_id=owner_id,
            original_name=original,
            stored_path=str(stored_path),
            mime=mime,
            size_bytes=size,
            page_count=len(pages),
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    meta.file_id,
                    owner_id,
                    original,
                    str(stored_path),
                    mime,
                    size,
                    len(pages),
                    json.dumps(pages, ensure_ascii=False),
                    meta.created_at,
                ),
            )
        self.logger.info(
            "file store file_id=%s pages=%s size=%s", file_id, len(pages), size
        )
        return meta

    def _extract_pdf_pages(self, path: Path) -> list[str]:
        from pypdf import PdfReader

        return [(pg.extract_text() or "") for pg in PdfReader(str(path)).pages]

    def _get_row(self, file_id: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM files WHERE file_id=?", (file_id,)
            ).fetchone()

    def _pages(self, file_id: str) -> list[str] | None:
        """Return the cached page-text list for ``file_id``, or None if absent."""
        row = self._get_row(file_id)
        if row is None:
            return None
        return json.loads(row["pages_json"])

    def get_info(self, file_id: str) -> FileInfo | None:
        """Return a :class:`FileInfo` snapshot, or None if the file is unknown."""
        row = self._get_row(file_id)
        if row is None:
            return None
        return FileInfo(
            file_id=row["file_id"],
            original_name=row["original_name"],
            mime=row["mime"],
            size_bytes=row["size_bytes"],
            page_count=row["page_count"],
        )

    def read_pages(self, file_id: str, start: int, end: int) -> str:
        """Return concatenated text for pages ``[start, end]`` (1-based, inclusive).

        On any failure (missing file, invalid range) returns a localized
        error string prefixed with ``[错误]`` so the caller can surface it
        verbatim without raising.
        """
        pages = self._pages(file_id)
        if pages is None:
            return f"[错误] 文件 {file_id} 不存在"
        total = len(pages)
        # Normalise to 1-based inclusive bounds; reject empty / reversed ranges.
        if start < 1 or end < 1 or start > end:
            return f"[错误] 页码范围无效，文件共 {total} 页"
        if start > total or end > total:
            return f"[错误] 页码范围无效，文件共 {total} 页"
        chunks: list[str] = []
        for page_no in range(start, end + 1):
            chunks.append(f"--- 第 {page_no} 页 ---\n{pages[page_no - 1]}")
        return "\n".join(chunks)

    def search(self, file_id: str, query: str) -> list[FileHit]:
        """Case-insensitive substring search across cached pages.

        Returns one :class:`FileHit` per occurrence (page, snippet) with the
        snippet centred on the match (±60 chars). Empty list if no match or
        the file is unknown.
        """
        if not query:
            return []
        pages = self._pages(file_id)
        if pages is None:
            return []
        needle = query.lower()
        window = 60
        hits: list[FileHit] = []
        for idx, page_text in enumerate(pages, start=1):
            lowered = page_text.lower()
            start_pos = 0
            while True:
                found = lowered.find(needle, start_pos)
                if found == -1:
                    break
                snippet_lo = max(0, found - window)
                snippet_hi = min(len(page_text), found + len(needle) + window)
                snippet = page_text[snippet_lo:snippet_hi]
                hits.append(FileHit(page=idx, snippet=snippet))
                start_pos = found + len(needle)
        return hits
