from __future__ import annotations

from fastapi import APIRouter

from ...logging_utils import get_component_logger
from ...models import IndexRequest, IndexStatsView
from ..deps import (
    apply_rag_settings,
    get_health_monitor,
    get_index_supervisor,
    get_store,
)

logger = get_component_logger("api")

router = APIRouter()


@router.post("/api/kb/index")
def index_knowledge_base(req: IndexRequest | None = None):
    """异步提交 reindex：立即返回当前任务状态，**不阻塞**等待 build 完成。

    worker 正忙时返回 running 状态（前端据 status 判断是否提示"正在索引中"）。
    """
    runtime_settings = apply_rag_settings(req)
    reset = req.reset if req else True
    logger.info("kb index submit reset=%s", reset)
    status = get_index_supervisor().submit(runtime_settings, reset=reset, source="manual")
    logger.info("kb index accepted status=%s", status.status)
    return status


@router.get("/api/kb/stats")
def kb_stats():
    """collection 统计 + RAG 任务状态（含 last_indexed_at / 进度）。前端轮询用。"""
    store = get_store(apply_rag_settings())
    base = store.ensure_index()
    status = get_index_supervisor().get_status()
    return IndexStatsView(**base.model_dump(), rag_index=status)


@router.get("/api/kb/search")
def search_knowledge_base(q: str, top_k: int | None = None):
    runtime = apply_rag_settings(top_k=top_k) if top_k else apply_rag_settings()
    store = get_store(runtime)
    results = store.search(q, top_k=top_k)
    logger.info("kb search query_chars=%s top_k=%s results=%s", len(q), top_k, len(results))
    return {"query": q, "results": results}


@router.get("/api/health")
def health():
    return get_health_monitor().snapshot()
