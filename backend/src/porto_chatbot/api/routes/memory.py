from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ...logging_utils import get_component_logger
from ...memory import SessionFactsStore
from ...models import MemorySearchResponse
from ..deps import current_settings, get_memory

logger = get_component_logger("api")

router = APIRouter()


class SessionItem(BaseModel):
    session_id: str
    first_at: str
    last_at: str
    message_count: int
    preview: str


class SessionListResponse(BaseModel):
    items: list[SessionItem]
    total: int
    has_more: bool


@router.get("/api/sessions", response_model=SessionListResponse)
def list_sessions(date: str | None = None, limit: int = 20, offset: int = 0):
    items, total = get_memory().list_sessions(date=date, limit=limit, offset=offset)
    return SessionListResponse(
        items=[SessionItem(**m) for m in items],
        total=total,
        has_more=offset + len(items) < total,
    )


@router.get("/api/memory/{session_id}")
def list_memory(session_id: str, limit: int = 50):
    logger.info("memory list session_id=%s limit=%s", session_id, limit)
    return {"session_id": session_id, "items": get_memory().list_session(session_id, limit=limit)}


@router.get("/api/memory/search", response_model=MemorySearchResponse)
def search_memory(q: str, session_id: str | None = None, top_k: int = 5):
    logger.info("memory search query_chars=%s session_id=%s top_k=%s", len(q), session_id, top_k)
    return MemorySearchResponse(query=q, results=get_memory().search(q, session_id=session_id, top_k=top_k))


@router.get("/api/memory/{session_id}/facts")
def list_session_facts(session_id: str):
    """返回 session 内 active facts 列表(按 category 优先级排序),供前端可观测。"""
    logger.info("memory facts session_id=%s", session_id)
    store = SessionFactsStore(current_settings())
    facts = store.list_active(session_id)
    return {"session_id": session_id, "facts": [f.model_dump() for f in facts]}
