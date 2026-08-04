from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ...logging_utils import get_component_logger
from ...models import ChatRequest, ChatResponse
from ..deps import apply_rag_settings, effective_rag_settings
from ..sse import _chat_request_from_stream_body

logger = get_component_logger("api")

router = APIRouter()


@router.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """Chatbot 入口——薄 dispatcher。

    把请求路由到 create_backend(scope='chatbot') 返回的引擎(默认 LangchainBackend)。
    runtime_settings 在这里 resolve(读 db 配置 + req.rag 覆盖),再交给后端;
    后端的 chat() / langchain_chat() 负责所有 intent routing / RAG / memory / facts。
    """
    logger.info(
        "chat start session_id=%s message_chars=%s top_k=%s",
        req.session_id,
        len(req.message),
        req.top_k,
    )
    rag_settings = effective_rag_settings(req.rag)
    top_k = req.top_k or rag_settings.top_k
    runtime_settings = apply_rag_settings(req.rag, agent=req.agent, top_k=top_k)

    from ...agent.factory import BackendScope, create_backend

    engine = create_backend(runtime_settings, scope=BackendScope.CHATBOT)
    # engine.chat() 是 async coroutine(走 LangchainBackend.chat → langchain_chat 同步实现)。
    # FastAPI 把 sync endpoint 跑在 threadpool,该线程无 event loop,asyncio.run 安全。
    return asyncio.run(engine.chat(req, runtime_settings))


@router.post("/api/chat/stream")
async def chat_stream(body: dict[str, Any]):
    """Chatbot 流式入口——薄 dispatcher。

    把 SSE 流交给 create_backend(scope='chatbot').chat_stream() 产生的 async iterator。
    """
    req = _chat_request_from_stream_body(body)
    logger.info("chat stream start session_id=%s", req.session_id)

    rag_settings = effective_rag_settings(req.rag)
    top_k = req.top_k or rag_settings.top_k
    runtime_settings = apply_rag_settings(req.rag, agent=req.agent, top_k=top_k)

    from ...agent.factory import BackendScope, create_backend

    engine = create_backend(runtime_settings, scope=BackendScope.CHATBOT)
    return StreamingResponse(
        engine.chat_stream(req, runtime_settings),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
