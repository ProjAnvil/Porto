from __future__ import annotations

from fastapi import APIRouter

from ...logging_utils import get_component_logger
from ...models import IndexRequest
from ..deps import apply_rag_settings, get_store

logger = get_component_logger("api")

router = APIRouter()


@router.post("/api/kb/index")
def index_knowledge_base(req: IndexRequest | None = None):
    runtime_settings = apply_rag_settings(req)
    reset = req.reset if req else True
    logger.info("kb index start reset=%s", reset)
    stats = get_store(runtime_settings).build(reset=reset)
    logger.info("kb index finish documents=%s chunks=%s", stats.documents, stats.chunks)
    return stats


@router.get("/api/kb/stats")
def kb_stats():
    logger.info("kb stats")
    return get_store(apply_rag_settings()).ensure_index()


@router.get("/api/kb/search")
def search_knowledge_base(q: str, top_k: int | None = None):
    store = get_store(apply_rag_settings(top_k=top_k) if top_k else apply_rag_settings())
    store.ensure_index()
    results = store.search(q, top_k=top_k)
    logger.info("kb search query_chars=%s top_k=%s results=%s", len(q), top_k, len(results))
    return {"query": q, "results": results}
