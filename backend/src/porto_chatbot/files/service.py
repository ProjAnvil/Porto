"""FileService — store uploaded documents with pagination metadata.

Durable home for user-uploaded PRDs/reference files. Each upload is written
under ``settings.files_dir/<file_id>/<original_name>`` and a row is inserted
into ``settings.files_db_path`` (SQLite) recording mime/size/page breakdown.
Pages come from PDF text extraction or virtual 2000-char chunking for other
text-like formats so downstream nodes can cite ``page N`` consistently.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from fastapi import UploadFile

from ..documents import parse_document
from ..logging_utils import get_component_logger
from ..models.file import FileMeta
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
        artifact = parse_document(
            stored_path,
            original_name=original,
            max_bytes=max_bytes,
            max_pdf_pages=max_pdf,
        )
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
