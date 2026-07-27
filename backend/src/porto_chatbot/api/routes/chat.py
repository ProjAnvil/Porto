from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ...evaluation import evaluate_rag_cases
from ...intent import IntentDecision, route_chat_intent
from ...llm import LLMClient, format_sources
from ...logging_utils import get_component_logger
from ...memory import (
    SessionFactsStore,
    build_facts_prompt,
    get_compacted_history,
    trigger_facts_extraction_async,
    trigger_facts_extraction_sync,
)
from ...models import ChatRequest, ChatResponse, EvalCase
from ..deps import (
    apply_rag_settings,
    effective_rag_settings,
    get_index_supervisor,
    get_memory,
    get_store,
)
from ..sse import _ai_sdk_sse, _chat_request_from_stream_body, _text_chunks

logger = get_component_logger("api")

router = APIRouter()


def _trim_to_budget(parts: list[str], budget: int) -> list[str]:
    """超字符预算时从后向前截断：保留问题/摘要/会话，裁剪 memories/sources。

    context engineering 的预算保护：避免长会话 + 大量检索片段撑爆 context 窗口。
    """
    if budget <= 0 or sum(len(p) for p in parts) <= budget:
        return parts
    suffix = "…（已截断）"
    result = list(parts)
    for i in range(len(result) - 1, -1, -1):
        total = sum(len(p) for p in result)
        if total <= budget:
            break
        over = total - budget
        part = result[i]
        keep = len(part) - over  # part i 最多保留的字符数
        if keep <= 0:
            result[i] = ""
        else:
            room = keep - len(suffix)
            result[i] = (part[:room] + suffix) if room > 0 else part[:keep]
    return [p for p in result if p]


def _direct_chat_answer(
    req: ChatRequest, runtime_settings, decision: IntentDecision, llm: LLMClient | None = None
) -> ChatResponse:
    llm = llm or LLMClient(runtime_settings)
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


_RAG_UNAVAILABLE_HINTS = {
    "reindexing": "知识库正在重建索引，请等待完成后再提问。",
    "index_unavailable": "知识库索引不可用，请在设置中触发重新索引后再提问。",
}


def _rag_unavailable_hint(reason: str | None) -> str:
    return _RAG_UNAVAILABLE_HINTS.get(reason or "", "知识库当前不可用，请稍后重试。")


def _rag_unavailable_answer(req: ChatRequest, reason: str | None) -> ChatResponse:
    hint = _rag_unavailable_hint(reason)
    logger.info("chat rag unavailable session_id=%s reason=%s", req.session_id, reason)
    return ChatResponse(
        answer=hint,
        sources=[],
        memory=[],
        evaluation={"score": 0.0, "passed": False, "cases": []},
        steps=[
            {
                "name": "route_intent",
                "status": "completed",
                "summary": f"rag unavailable: {reason}",
                "data": {"reason": reason},
            },
            {
                "name": "retrieve_knowledge",
                "status": "skipped",
                "summary": hint,
                "data": {},
            },
            {
                "name": "answer",
                "status": "completed",
                "summary": "RAG 不可用，返回提示",
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
    llm = LLMClient(runtime_settings)
    decision = route_chat_intent(req.message, runtime_settings, llm)
    if decision.intent == "direct":
        return _direct_chat_answer(req, runtime_settings, decision, llm)

    available, reason = get_index_supervisor().rag_available()
    if not available:
        return _rag_unavailable_answer(req, reason)

    store = get_store(runtime_settings)
    memory = get_memory(runtime_settings)
    store.ensure_index()
    sources = store.search(req.message, top_k=top_k)
    memories = memory.search(req.message, session_id=req.session_id, top_k=5)
    summary, recent = get_compacted_history(req.session_id, memory, llm)
    memory.add(session_id=req.session_id, role="user", content=req.message)

    # Session facts(最高优先级注入):active facts 拼成 prompt 片段插在用户问题之后;
    # 同步 fire-and-forget 触发 LLM 提取(daemon 线程,不阻塞响应)。
    # facts 读取 fail-open:任何异常(db 锁/磁盘满/老 db 缺 migration)都降级为空串,
    # 不阻塞主 chat 链路。
    facts_store = SessionFactsStore(runtime_settings)
    facts_block = ""
    if runtime_settings.facts_enabled:
        try:
            facts_block = build_facts_prompt(facts_store.by_category(req.session_id))
        except Exception:
            logger.exception("facts load failed session=%s", req.session_id)
            facts_block = ""
    trigger_facts_extraction_sync(
        store=facts_store,
        llm=llm,
        session_id=req.session_id,
        new_message=req.message,
        recent_turns=recent,
        settings=runtime_settings,
    )

    prompt_parts = [f"用户问题:\n{req.message}"]
    if facts_block:
        prompt_parts.append(facts_block)
    if summary:
        prompt_parts.append(f"会话历史摘要:\n{summary}")
    prompt_parts.append("最近会话:\n" + "\n".join(f"{m.role}: {m.content}" for m in recent))
    prompt_parts.append(f"记忆检索:\n{format_sources(memories)}")
    prompt_parts.append(f"知识库片段:\n{format_sources(sources)}")
    prompt_parts = _trim_to_budget(prompt_parts, runtime_settings.context_char_budget)
    answer = llm.complete(
        "你是 Porto 知识库问答助手。优先基于知识库片段回答，也可引用会话记忆；不确定时说明缺口。",
        "\n\n".join(prompt_parts),
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
                "summary": f"检索到 {len(memories)} 条记忆，近期 {len(recent)} 条"
                + ("（含历史摘要）" if summary else ""),
                "data": {
                    "compacted": bool(summary),
                    "recent": len(recent),
                    "memory_hits": len(memories),
                },
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

    rag_settings = effective_rag_settings(req.rag)
    top_k = req.top_k or rag_settings.top_k
    runtime_settings = apply_rag_settings(req.rag, agent=req.agent, top_k=top_k)
    llm = LLMClient(runtime_settings)
    decision = route_chat_intent(req.message, runtime_settings, llm)

    async def events() -> AsyncIterator[str]:
        text_id = "answer-1"
        try:
            if decision.intent == "direct":
                response = _direct_chat_answer(req, runtime_settings, decision, llm)
                yield _ai_sdk_sse(
                    {"type": "start", "messageMetadata": {"session_id": req.session_id}}
                )
                yield _ai_sdk_sse({"type": "start-step"})
                yield _ai_sdk_sse({"type": "text-start", "id": text_id})
                for chunk in _text_chunks(response.answer):
                    yield _ai_sdk_sse({"type": "text-delta", "id": text_id, "delta": chunk})
                yield _ai_sdk_sse({"type": "text-end", "id": text_id})
                yield _ai_sdk_sse({"type": "finish-step"})
                yield _ai_sdk_sse(
                    {
                        "type": "finish",
                        "finishReason": "stop",
                        "messageMetadata": {"source_count": 0},
                    }
                )
                yield "data: [DONE]\n\n"
                return

            available, reason = get_index_supervisor().rag_available()
            if not available:
                hint = _rag_unavailable_hint(reason)
                yield _ai_sdk_sse(
                    {"type": "start", "messageMetadata": {"session_id": req.session_id}}
                )
                yield _ai_sdk_sse({"type": "start-step"})
                yield _ai_sdk_sse({"type": "text-start", "id": text_id})
                for chunk in _text_chunks(hint):
                    yield _ai_sdk_sse({"type": "text-delta", "id": text_id, "delta": chunk})
                yield _ai_sdk_sse({"type": "text-end", "id": text_id})
                yield _ai_sdk_sse({"type": "finish-step"})
                yield _ai_sdk_sse(
                    {
                        "type": "finish",
                        "finishReason": "stop",
                        "messageMetadata": {"source_count": 0},
                    }
                )
                yield "data: [DONE]\n\n"
                logger.info(
                    "chat stream rag unavailable session_id=%s reason=%s", req.session_id, reason
                )
                return

            store = get_store(runtime_settings)
            memory = get_memory(runtime_settings)
            store.ensure_index()
            sources = store.search(req.message, top_k=top_k)
            memories = memory.search(req.message, session_id=req.session_id, top_k=5)
            summary, recent = get_compacted_history(req.session_id, memory, llm)
            memory.add(session_id=req.session_id, role="user", content=req.message)

            # Session facts(最高优先级注入):active facts 拼成 prompt 片段插在用户问题之后;
            # 异步 fire-and-forget 触发 LLM 提取(asyncio.create_task + to_thread,
            # 不阻塞 SSE 流)。
            # facts 读取 fail-open:任何异常(db 锁/磁盘满/老 db 缺 migration)都降级为空串,
            # 不阻塞主 chat 链路。
            facts_store = SessionFactsStore(runtime_settings)
            facts_block = ""
            if runtime_settings.facts_enabled:
                try:
                    facts_block = build_facts_prompt(facts_store.by_category(req.session_id))
                except Exception:
                    logger.exception("facts load failed session=%s", req.session_id)
                    facts_block = ""
            trigger_facts_extraction_async(
                store=facts_store,
                llm=llm,
                session_id=req.session_id,
                new_message=req.message,
                recent_turns=recent,
                settings=runtime_settings,
            )

            prompt_parts = [f"用户问题:\n{req.message}"]
            if facts_block:
                prompt_parts.append(facts_block)
            if summary:
                prompt_parts.append(f"会话历史摘要:\n{summary}")
            prompt_parts.append("最近会话:\n" + "\n".join(f"{m.role}: {m.content}" for m in recent))
            prompt_parts.append(f"记忆检索:\n{format_sources(memories)}")
            prompt_parts.append(f"知识库片段:\n{format_sources(sources)}")
            prompt_parts = _trim_to_budget(prompt_parts, runtime_settings.context_char_budget)
            system_prompt = "你是 Porto 知识库问答助手。优先基于知识库片段回答，也可引用会话记忆；不确定时说明缺口。"

            yield _ai_sdk_sse({"type": "start", "messageMetadata": {"session_id": req.session_id}})
            yield _ai_sdk_sse({"type": "start-step"})
            yield _ai_sdk_sse({"type": "text-start", "id": text_id})

            answer = ""
            streamed = runtime_settings.agent_stream_enabled and llm.enabled
            if streamed:
                for delta in llm.stream(system_prompt, "\n\n".join(prompt_parts)):
                    if delta:
                        answer += delta
                        yield _ai_sdk_sse({"type": "text-delta", "id": text_id, "delta": delta})
            else:
                answer = llm.complete(system_prompt, "\n\n".join(prompt_parts)) or ""
                if not answer:
                    if sources:
                        bullets = "\n".join(
                            f"- [{i + 1}] {s.path}: {s.text[:180].replace(chr(10), ' ')}"
                            for i, s in enumerate(sources[:4])
                        )
                        answer = f"我在知识库中找到以下相关内容：\n{bullets}\n\n基于这些片段，建议优先查看匹配分最高的文档并补充更具体的问题。"
                    else:
                        answer = "当前知识库没有检索到相关片段。请先执行知识库索引，或确认 `~/.scv/analysis` 中存在 md/txt/pdf/docx 文件。"
                for chunk in _text_chunks(answer):
                    yield _ai_sdk_sse({"type": "text-delta", "id": text_id, "delta": chunk})

            yield _ai_sdk_sse({"type": "text-end", "id": text_id})

            if answer:
                memory.add(session_id=req.session_id, role="assistant", content=answer)
            evaluation = evaluate_rag_cases(
                [EvalCase(question=req.message, answer=answer, contexts=[s.text for s in sources])]
            )
            for source in sources[:6]:
                yield _ai_sdk_sse(
                    {
                        "type": "source-document",
                        "sourceId": source.id,
                        "mediaType": "text/plain",
                        "title": source.title or source.path,
                        "filename": source.path,
                    }
                )
            steps = [
                {
                    "name": "route_intent",
                    "status": "completed",
                    "summary": f"rag: {decision.reason}",
                    "data": {"intent": decision.intent, "reason": decision.reason},
                },
                {
                    "name": "retrieve_memory",
                    "status": "completed",
                    "summary": f"检索到 {len(memories)} 条记忆，近期 {len(recent)} 条"
                    + ("（含历史摘要）" if summary else ""),
                    "data": {
                        "compacted": bool(summary),
                        "recent": len(recent),
                        "memory_hits": len(memories),
                    },
                },
                {
                    "name": "retrieve_knowledge",
                    "status": "completed",
                    "summary": f"检索到 {len(sources)} 个片段",
                    "data": {},
                },
                {
                    "name": "answer",
                    "status": "completed",
                    "summary": "完成回答生成",
                    "data": {"streamed": streamed},
                },
                {
                    "name": "evaluate_rag",
                    "status": "completed",
                    "summary": f"RAG eval score {evaluation['score']}",
                    "data": evaluation,
                },
            ]
            yield _ai_sdk_sse(
                {
                    "type": "data-porto",
                    "id": "porto-inspector",
                    "transient": True,
                    "data": {
                        "steps": steps,
                        "sources": [s.model_dump() for s in sources],
                        "memory": [m.model_dump() for m in memories],
                        "evaluation": evaluation,
                        "workflow": None,
                    },
                }
            )
            yield _ai_sdk_sse({"type": "finish-step"})
            yield _ai_sdk_sse(
                {
                    "type": "finish",
                    "finishReason": "stop",
                    "messageMetadata": {"evaluation": evaluation, "source_count": len(sources)},
                }
            )
            yield "data: [DONE]\n\n"
            logger.info(
                "chat stream finish session_id=%s sources=%s streamed=%s",
                req.session_id,
                len(sources),
                streamed,
            )
        except Exception as exc:
            logger.exception("chat stream failed session_id=%s", req.session_id)
            yield _ai_sdk_sse({"type": "error", "errorText": str(exc)})
            yield _ai_sdk_sse({"type": "finish", "finishReason": "error"})
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
