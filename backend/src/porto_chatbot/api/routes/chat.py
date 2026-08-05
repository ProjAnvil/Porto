from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from ...documents import (
    SUPPORTED_EXTENSIONS,
    DocumentLimitError,
    DocumentNativeError,
    DocumentParseError,
)
from ...logging_utils import get_component_logger
from ...models import ChatRequest, ChatResponse
from ..deps import apply_rag_settings, effective_rag_settings, get_file_service
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


@router.post("/api/chat/files")
def upload_chat_file(
    file: Annotated[UploadFile, File()],
    session_id: Annotated[str, Form()] = "default",
):
    """聊天附件上传 —— 落盘 + 分页 + 元数据,返回 file_id 供后续 chat 复用。

    M7: 同步 ``def`` 让 FastAPI 把 I/O 跑在线程池(避免阻塞事件循环);
    FileService.store 内部是同步文件 + SQLite 操作。大小/扩展名预校验沿用
    workflow upload 路由的 short-circuit,避免把超大文件读进内存。
    """
    from pathlib import Path

    runtime_settings = apply_rag_settings()
    suffix = Path(file.filename or "").suffix.lower()
    if not suffix:
        raise HTTPException(400, "uploaded file must have an extension")
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(415, f"unsupported document type: {suffix}")
    # FileService.store 内部 read 无上限,需先 short-circuit 拦超大文件。
    max_bytes = runtime_settings.document_max_upload_mb * 1024 * 1024
    payload = file.file.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise HTTPException(
            413,
            f"document exceeds {runtime_settings.document_max_upload_mb} MB upload limit",
        )
    file.file.seek(0)

    sid = session_id or "default"
    try:
        meta = get_file_service().store(file, owner_id=sid)
    except DocumentLimitError as exc:
        raise HTTPException(413, str(exc)) from exc
    except DocumentNativeError as exc:
        raise HTTPException(422, str(exc)) from exc
    except DocumentParseError as exc:
        raise HTTPException(400, str(exc)) from exc

    logger.info(
        "chat file upload session_id=%s filename=%s file_id=%s pages=%s",
        sid,
        meta.original_name,
        meta.file_id,
        meta.page_count,
    )
    return {
        "file_id": meta.file_id,
        "page_count": meta.page_count,
        "original_name": meta.original_name,
    }
