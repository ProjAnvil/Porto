"""Session / Message API routes.

从 memory.py 演化而来：sessions 是一等实体，messages 是子资源。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..deps import current_settings, get_session_store
from ...models import MessageRecord, SessionFact

router = APIRouter(prefix="/api", tags=["sessions"])


class SessionItem(BaseModel):
    session_id: str
    title: str | None = None
    first_at: str
    last_at: str
    message_count: int
    preview: str


class SessionListResponse(BaseModel):
    items: list[SessionItem]
    total: int
    has_more: bool


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(date: str | None = None, limit: int = 20, offset: int = 0):
    items, total = get_session_store().list_sessions(date=date, limit=limit, offset=offset)
    return SessionListResponse(
        items=[SessionItem(**item) for item in items],
        total=total,
        has_more=(offset + limit) < total,
    )


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    store = get_session_store()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    return {
        "session_id": session.id,
        "title": session.title,
        "status": session.status,
        "created_at": session.created_at,
        "last_active_at": session.last_active_at,
    }


@router.get("/sessions/{session_id}/messages")
def list_messages(session_id: str, limit: int = 50):
    store = get_session_store()
    items: list[MessageRecord] = store.list_messages(session_id, limit=limit)
    return {"session_id": session_id, "items": [item.model_dump() for item in items]}


@router.get("/sessions/{session_id}/facts")
def get_session_facts(session_id: str):
    from ...memory import SessionFactsStore

    store = SessionFactsStore(current_settings())
    grouped = store.by_category(session_id)
    # Flatten grouped dict to list (by_category already orders within each category
    # by updated_at DESC; across categories order is unspecified — callers that
    # need priority order should use SessionFactsStore.list_active directly).
    facts: list[SessionFact] = []
    for cat_facts in grouped.values():
        facts.extend(cat_facts)
    return {"session_id": session_id, "facts": [f.model_dump() for f in facts]}
