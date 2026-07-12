from __future__ import annotations

import json
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .agent import PortoAgent
from .config_store import ConfigStore
from .documents import read_document
from .evaluation import evaluate_rag_cases
from .intent import IntentDecision, route_chat_intent
from .llm import LLMClient, format_sources
from .logging_utils import get_component_logger
from .memory import MemoryStore
from .models import (
    AgentSettingsPayload,
    AppSettingsPayload,
    AppSettingsResponse,
    ChatRequest,
    ChatResponse,
    EvalCase,
    EvalRequest,
    IndexRequest,
    MemorySearchResponse,
    RagSettingsPayload,
    WorkflowRequest,
    WorkflowResponse,
)
from .settings import settings
from .vector_store import LocalVectorStore

logger = get_component_logger("api")

app = FastAPI(title="Porto Chatbot API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    logger.info("request start method=%s path=%s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.exception(
            "request failed method=%s path=%s elapsed_ms=%s",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info(
        "request finish method=%s path=%s status=%s elapsed_ms=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


def get_config_store() -> ConfigStore:
    return ConfigStore(settings)


def default_rag_settings() -> RagSettingsPayload:
    return RagSettingsPayload(
        embedding_provider=settings.embedding_provider,
        embedding_model=settings.embedding_model,
        embedding_base_url=settings.embedding_base_url,
        chunk_size=settings.max_chunk_chars,
        chunk_overlap=settings.chunk_overlap,
        top_k=settings.top_k,
    )


def default_agent_settings() -> AgentSettingsPayload:
    return AgentSettingsPayload(
        agent_provider=settings.agent_provider,
        agent_model=settings.agent_model,
        agent_base_url=settings.agent_base_url,
        agent_api_key=settings.agent_api_key,
        agent_temperature=settings.agent_temperature,
        agent_max_tokens=settings.agent_max_tokens,
    )


def effective_rag_settings(payload: RagSettingsPayload | None = None) -> RagSettingsPayload:
    updates = default_rag_settings().model_dump(exclude_none=True)
    updates.update(get_config_store().get_rag_settings().model_dump(exclude_none=True))
    if payload:
        updates.update(payload.model_dump(exclude_none=True))
    return RagSettingsPayload(**updates)


def effective_agent_settings(payload: AgentSettingsPayload | None = None) -> AgentSettingsPayload:
    updates = default_agent_settings().model_dump(exclude_none=True)
    updates.update(get_config_store().get_agent_settings().model_dump(exclude_none=True))
    if payload:
        updates.update(payload.model_dump(exclude_none=True))
    return AgentSettingsPayload(**updates)


def apply_rag_settings(
    payload: RagSettingsPayload | None = None,
    agent: AgentSettingsPayload | None = None,
    **extra,
):
    updates = effective_agent_settings(agent).model_dump(exclude_none=True)
    updates.update(effective_rag_settings(payload).model_dump(exclude_none=True))
    updates.update(extra)
    if "chunk_size" in updates:
        updates["max_chunk_chars"] = updates.pop("chunk_size")
    return settings.model_copy(update=updates)


def get_store(runtime_settings=None) -> LocalVectorStore:
    return LocalVectorStore(runtime_settings or settings)


def get_agent() -> PortoAgent:
    return PortoAgent(settings, get_store(), LLMClient(settings))


def get_memory(runtime_settings=None) -> MemoryStore:
    return MemoryStore(runtime_settings or settings)


def _extract_text_from_ai_sdk_part(part: dict[str, Any]) -> str:
    if part.get("type") == "text":
        return str(part.get("text") or "")
    return ""


def _extract_latest_user_message(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        parts = message.get("parts")
        if isinstance(parts, list):
            return "\n".join(
                _extract_text_from_ai_sdk_part(part)
                for part in parts
                if isinstance(part, dict)
            ).strip()
    return ""


def _chat_request_from_stream_body(body: dict[str, Any]) -> ChatRequest:
    if "message" in body:
        return ChatRequest.model_validate(body)
    message = _extract_latest_user_message(body.get("messages") or [])
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    return ChatRequest(
        message=message,
        session_id=body.get("session_id") or body.get("sessionId") or body.get("id") or "default",
        top_k=body.get("top_k"),
        rag=body.get("rag"),
        agent=body.get("agent"),
    )


def _ai_sdk_sse(part: dict[str, Any]) -> str:
    return f"data: {json.dumps(part, ensure_ascii=False)}\n\n"


def _text_chunks(text: str, chunk_size: int = 48) -> list[str]:
    if not text:
        return [""]
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def _direct_chat_answer(req: ChatRequest, runtime_settings, decision: IntentDecision) -> ChatResponse:
    llm = LLMClient(runtime_settings)
    answer = llm.complete(
        "你是 Porto 助手。用户当前消息不需要检索知识库，直接、简洁、友好地回应。",
        f"用户消息:\n{req.message}",
    )
    if not answer:
        if decision.reason == "greeting":
            answer = "你好！我是 Porto 助手，可以帮你查询知识库、拆解 PRD，或生成子系统需求。"
        elif decision.reason == "smalltalk_or_help":
            answer = "我是 Porto 助手，可以进行知识库问答、PRD 分析和子系统拆分。你可以直接描述需求，或在 Settings 里重新索引知识库。"
        else:
            answer = "我在。你可以继续提问，或说明需要查询哪部分知识库内容。"

    evaluation = {"score": 0.0, "passed": True, "cases": []}
    logger.info(
        "chat direct finish session_id=%s reason=%s answer_chars=%s",
        req.session_id,
        decision.reason,
        len(answer),
    )
    return ChatResponse(
        answer=answer,
        sources=[],
        memory=[],
        evaluation=evaluation,
        steps=[
            {
                "name": "route_intent",
                "status": "completed",
                "summary": f"direct: {decision.reason}",
                "data": {"intent": decision.intent, "reason": decision.reason},
            },
            {
                "name": "answer",
                "status": "completed",
                "summary": "直接回复，未调用 RAG",
                "data": {},
            },
        ],
    )


@app.get("/api/health")
def health() -> dict:
    rag_settings = effective_rag_settings()
    agent_settings = effective_agent_settings()
    return {
        "ok": True,
        "kb_path": str(settings.kb_path),
        "data_dir": str(settings.data_dir),
        "rag": {
            "vector_backend": settings.vector_backend,
            "embedding_provider": rag_settings.embedding_provider,
            "embedding_model": rag_settings.embedding_model,
            "embedding_base_url": rag_settings.embedding_base_url,
            "chunk_size": rag_settings.chunk_size,
            "chunk_overlap": rag_settings.chunk_overlap,
            "top_k": rag_settings.top_k,
        },
        "agent": {
            "agent_provider": agent_settings.agent_provider,
            "agent_model": agent_settings.agent_model,
            "agent_base_url": agent_settings.agent_base_url,
            "agent_temperature": agent_settings.agent_temperature,
            "agent_max_tokens": agent_settings.agent_max_tokens,
        },
    }


@app.get("/api/settings", response_model=AppSettingsResponse)
def get_app_settings():
    logger.info("settings read")
    return AppSettingsResponse(
        rag=effective_rag_settings(),
        agent=effective_agent_settings(),
    )


@app.put("/api/settings", response_model=AppSettingsResponse)
def save_app_settings(req: AppSettingsPayload):
    store = get_config_store()
    if req.rag:
        logger.info("settings save namespace=rag")
        store.save_rag_settings(req.rag)
    if req.agent:
        logger.info("settings save namespace=agent provider=%s model=%s", req.agent.agent_provider, req.agent.agent_model)
        store.save_agent_settings(req.agent)
    return get_app_settings()


@app.post("/api/kb/index")
def index_knowledge_base(req: IndexRequest | None = None):
    runtime_settings = apply_rag_settings(req)
    reset = req.reset if req else True
    logger.info("kb index start reset=%s", reset)
    stats = get_store(runtime_settings).build(reset=reset)
    logger.info("kb index finish documents=%s chunks=%s", stats.documents, stats.chunks)
    return stats


@app.get("/api/kb/stats")
def kb_stats():
    logger.info("kb stats")
    return get_store(apply_rag_settings()).ensure_index()


@app.get("/api/kb/search")
def search_knowledge_base(q: str, top_k: int | None = None):
    store = get_store(apply_rag_settings(top_k=top_k) if top_k else apply_rag_settings())
    store.ensure_index()
    results = store.search(q, top_k=top_k)
    logger.info("kb search query_chars=%s top_k=%s results=%s", len(q), top_k, len(results))
    return {"query": q, "results": results}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    logger.info(
        "chat start session_id=%s message_chars=%s top_k=%s",
        req.session_id,
        len(req.message),
        req.top_k,
    )
    rag_settings = effective_rag_settings(req.rag)
    top_k = req.top_k or rag_settings.top_k
    runtime_settings = apply_rag_settings(req.rag, agent=req.agent, top_k=top_k)
    decision = route_chat_intent(req.message, runtime_settings)
    if decision.intent == "direct":
        return _direct_chat_answer(req, runtime_settings, decision)

    store = get_store(runtime_settings)
    memory = get_memory(runtime_settings)
    store.ensure_index()
    sources = store.search(req.message, top_k=top_k)
    memories = memory.search(req.message, session_id=req.session_id, top_k=5)
    previous = memory.list_session(req.session_id, limit=8)
    memory.add(session_id=req.session_id, role="user", content=req.message)

    llm = LLMClient(runtime_settings)
    answer = llm.complete(
        "你是 Porto 知识库问答助手。优先基于知识库片段回答，也可引用会话记忆；不确定时说明缺口。",
        "\n\n".join(
            [
                f"用户问题:\n{req.message}",
                "最近会话:\n"
                + "\n".join(f"{m.role}: {m.content}" for m in reversed(previous[:6])),
                f"记忆检索:\n{format_sources(memories)}",
                f"知识库片段:\n{format_sources(sources)}",
            ]
        ),
    )
    if not answer:
        if sources:
            bullets = "\n".join(
                f"- [{i + 1}] {s.path}: {s.text[:180].replace(chr(10), ' ')}"
                for i, s in enumerate(sources[:4])
            )
            answer = f"我在知识库中找到以下相关内容：\n{bullets}\n\n基于这些片段，建议优先查看匹配分最高的文档并补充更具体的问题。"
        else:
            answer = "当前知识库没有检索到相关片段。请先执行知识库索引，或确认 `~/.scv/analysis` 中存在 md/txt/pdf/docx 文件。"
    memory.add(session_id=req.session_id, role="assistant", content=answer)
    evaluation = evaluate_rag_cases(
        [
            EvalCase(
                question=req.message,
                answer=answer,
                contexts=[source.text for source in sources],
            )
        ]
    )
    logger.info(
        "chat finish session_id=%s sources=%s memories=%s score=%s answer_chars=%s",
        req.session_id,
        len(sources),
        len(memories),
        evaluation["score"],
        len(answer),
    )
    return ChatResponse(
        answer=answer,
        sources=sources,
        memory=memories,
        evaluation=evaluation,
        steps=[
            {
                "name": "route_intent",
                "status": "completed",
                "summary": f"rag: {decision.reason}",
                "data": {"intent": decision.intent, "reason": decision.reason},
            },
            {
                "name": "retrieve_memory",
                "status": "completed",
                "summary": f"检索到 {len(memories)} 条记忆",
                "data": {},
            },
            {
                "name": "retrieve_knowledge",
                "status": "completed",
                "summary": f"检索到 {len(sources)} 个片段",
                "data": {},
            },
            {"name": "answer", "status": "completed", "summary": "完成回答生成", "data": {}},
            {
                "name": "evaluate_rag",
                "status": "completed",
                "summary": f"RAG eval score {evaluation['score']}",
                "data": evaluation,
            },
        ],
    )


@app.post("/api/chat/stream")
async def chat_stream(body: dict[str, Any]):
    req = _chat_request_from_stream_body(body)
    logger.info("chat stream start session_id=%s", req.session_id)

    async def events() -> AsyncIterator[str]:
        try:
            response = chat(req)
        except Exception as exc:
            logger.exception("chat stream failed session_id=%s", req.session_id)
            yield _ai_sdk_sse({"type": "error", "errorText": str(exc)})
            yield _ai_sdk_sse({"type": "finish", "finishReason": "error"})
            yield "data: [DONE]\n\n"
            return

        text_id = "answer-1"
        yield _ai_sdk_sse({"type": "start", "messageMetadata": {"session_id": req.session_id}})
        yield _ai_sdk_sse({"type": "start-step"})
        yield _ai_sdk_sse({"type": "text-start", "id": text_id})
        for chunk in _text_chunks(response.answer):
            yield _ai_sdk_sse({"type": "text-delta", "id": text_id, "delta": chunk})
        yield _ai_sdk_sse({"type": "text-end", "id": text_id})
        for source in response.sources[:6]:
            yield _ai_sdk_sse(
                {
                    "type": "source-document",
                    "sourceId": source.id,
                    "mediaType": "text/plain",
                    "title": source.title or source.path,
                    "filename": source.path,
                }
            )
        yield _ai_sdk_sse(
            {
                "type": "data-porto",
                "id": "porto-inspector",
                "transient": True,
                "data": {
                    "steps": [step.model_dump() for step in response.steps],
                    "sources": [source.model_dump() for source in response.sources],
                    "memory": [memory.model_dump() for memory in response.memory],
                    "evaluation": response.evaluation,
                    "workflow": None,
                },
            }
        )
        yield _ai_sdk_sse({"type": "finish-step"})
        yield _ai_sdk_sse(
            {
                "type": "finish",
                "finishReason": "stop",
                "messageMetadata": {
                    "evaluation": response.evaluation,
                    "source_count": len(response.sources),
                },
            }
        )
        yield "data: [DONE]\n\n"
        logger.info(
            "chat stream finish session_id=%s sources=%s",
            req.session_id,
            len(response.sources),
        )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/porto/workflows", response_model=WorkflowResponse)
def run_porto_workflow(req: WorkflowRequest):
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    logger.info(
        "workflow start session_id=%s text_chars=%s top_k=%s",
        req.session_id,
        len(req.text),
        req.top_k,
    )
    rag_settings = effective_rag_settings(req.rag)
    top_k = req.top_k or rag_settings.top_k
    runtime_settings = apply_rag_settings(req.rag, agent=req.agent, top_k=top_k)
    get_memory(runtime_settings).add(
        session_id=req.session_id,
        role="user",
        content=req.text[:2000],
        metadata={"kind": "workflow_request"},
    )
    agent = PortoAgent(runtime_settings, get_store(runtime_settings), LLMClient(runtime_settings))
    response = agent.run(req.text, project_name=req.project_name, top_k=top_k)
    get_memory(runtime_settings).add(
        session_id=req.session_id,
        role="assistant",
        content=f"Workflow {response.workflow_id}: {', '.join(s.name for s in response.subsystems)}",
        metadata={"kind": "workflow_response", "workflow_id": response.workflow_id},
    )
    logger.info(
        "workflow finish session_id=%s workflow_id=%s subsystems=%s",
        req.session_id,
        response.workflow_id,
        len(response.subsystems),
    )
    return response


@app.post("/api/porto/workflows/upload", response_model=WorkflowResponse)
async def run_porto_workflow_upload(
    file: Annotated[UploadFile, File()],
    project_name: Annotated[str | None, Form()] = None,
    top_k: Annotated[int | None, Form()] = None,
):
    suffix = Path(file.filename or "").suffix
    if not suffix:
        raise HTTPException(status_code=400, detail="uploaded file must have an extension")
    logger.info("workflow upload start filename=%s suffix=%s top_k=%s", file.filename, suffix, top_k)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        text = read_document(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if not text.strip():
        raise HTTPException(status_code=400, detail="document has no extractable text")
    rag_settings = effective_rag_settings()
    resolved_top_k = top_k or rag_settings.top_k
    runtime_settings = apply_rag_settings(top_k=resolved_top_k)
    agent = PortoAgent(runtime_settings, get_store(runtime_settings), LLMClient(runtime_settings))
    response = agent.run(text, project_name=project_name, top_k=resolved_top_k)
    logger.info(
        "workflow upload finish filename=%s workflow_id=%s text_chars=%s",
        file.filename,
        response.workflow_id,
        len(text),
    )
    return response


@app.get("/api/memory/{session_id}")
def list_memory(session_id: str, limit: int = 50):
    logger.info("memory list session_id=%s limit=%s", session_id, limit)
    return {"session_id": session_id, "items": get_memory().list_session(session_id, limit=limit)}


@app.get("/api/memory/search", response_model=MemorySearchResponse)
def search_memory(q: str, session_id: str | None = None, top_k: int = 5):
    logger.info("memory search query_chars=%s session_id=%s top_k=%s", len(q), session_id, top_k)
    return MemorySearchResponse(query=q, results=get_memory().search(q, session_id=session_id, top_k=top_k))


@app.post("/api/eval/rag")
def evaluate_rag(req: EvalRequest):
    logger.info("eval rag cases=%s", len(req.cases))
    return evaluate_rag_cases(req.cases)
