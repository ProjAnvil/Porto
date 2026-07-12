from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ...evaluation import evaluate_rag_cases
from ...intent import IntentDecision, route_chat_intent
from ...llm import LLMClient, format_sources
from ...logging_utils import get_component_logger
from ...models import ChatRequest, ChatResponse, EvalCase
from ..deps import apply_rag_settings, effective_rag_settings, get_memory, get_store
from ..sse import _ai_sdk_sse, _chat_request_from_stream_body, _text_chunks

logger = get_component_logger("api")

router = APIRouter()


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


@router.post("/api/chat", response_model=ChatResponse)
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


@router.post("/api/chat/stream")
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
