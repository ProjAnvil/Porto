from __future__ import annotations

from fastapi import APIRouter

from ...logging_utils import get_component_logger
from ...models import MemorySearchResponse
from ..deps import get_memory

logger = get_component_logger("api")

router = APIRouter()


@router.get("/api/memory/{session_id}")
def list_memory(session_id: str, limit: int = 50):
    logger.info("memory list session_id=%s limit=%s", session_id, limit)
    return {"session_id": session_id, "items": get_memory().list_session(session_id, limit=limit)}


@router.get("/api/memory/search", response_model=MemorySearchResponse)
def search_memory(q: str, session_id: str | None = None, top_k: int = 5):
    logger.info("memory search query_chars=%s session_id=%s top_k=%s", len(q), session_id, top_k)
    return MemorySearchResponse(query=q, results=get_memory().search(q, session_id=session_id, top_k=top_k))
