from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class FileMeta(BaseModel):
    file_id: str
    owner_id: str
    original_name: str
    stored_path: str
    mime: str
    size_bytes: int
    page_count: int
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class FileInfo(BaseModel):
    file_id: str
    original_name: str
    mime: str
    size_bytes: int
    page_count: int


class FileHit(BaseModel):
    page: int
    snippet: str
