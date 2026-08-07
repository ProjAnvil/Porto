# backend/src/porto_chatbot/agent/langchain_chat_stream.py
"""ChatOrchestrator 的 SSE 流式实现。

从原 ``langchain_chat.py`` 的 ``langchain_chat_stream`` + 子生成器搬入，修改点：
- ``memory.add(role="user", ...)`` → ``orch.sessions.add_message(role="user", intent=..., indexed=False)``
- ``memory.search(...)`` → ``orch.memory.search(..., session_id=...)``（session_id 必填）
- ``get_compacted_history(session_id, memory, llm)`` → ``get_compacted_history(session_id, orch.sessions, llm)``
- DIRECT 路径：流结束后 ``persist_turn(..., index_vector=False)`` + ``maybe_generate_title(...)``
- RAG 不可用路径：流结束后 ``persist_turn(..., intent="rag", index_vector=False)`` + 标题
- RAG 路径：流前写 user msg；流后写 assistant msg + ``index_and_mark(...)``
- SSE 事件格式（``_ai_sdk_sse`` / ``_text_chunks``）完全不变
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ..api.sse import _ai_sdk_sse, _text_chunks
from ..evaluation import evaluate_rag_cases
from ..llm import LLMClient, format_sources  # noqa: F401 (LLMClient for type hint only)
from ..logging_utils import get_component_logger
from ..memory import (
    SessionFactsStore,
    build_facts_prompt,
    get_compacted_history,
    index_and_mark,
    maybe_generate_title,
    persist_turn,
    trigger_facts_extraction_async,
)
from ..models import ChatRequest, EvalCase
from ..models.enums import ChatIntent
from ..query_transform import retrieve_with_transform
from .orchestrator import ChatOrchestrator, _trim_to_budget

logger = get_component_logger("orchestrator")

_SOURCE_PREVIEW_CHARS = 180
_MAX_FALLBACK_SOURCES = 4
_MAX_SSE_SOURCES = 6


def _build_direct_answer(orch: ChatOrchestrator, req: ChatRequest, decision, llm: LLMClient) -> str:
    """与 ``ChatOrchestrator._handle_direct`` 同样的 LLM 调用 + 降级文案，但不持久化。"""
    answer = orch._llm_complete(
        "你是 Porto 助手。用户当前消息不需要检索知识库，直接、简洁、友好地回应。",
        f"用户消息:\n{req.message}",
        llm,
    )
    if not answer:
        if decision.reason == "greeting":
            answer = "你好！我是 Porto 助手，可以帮你查询知识库、拆解 PRD，或生成子系统需求。"
        elif decision.reason == "smalltalk_or_help":
            answer = "我是 Porto 助手，可以进行知识库问答、PRD 分析和子系统拆分。"
        else:
            answer = "我在。你可以继续提问，或说明需要查询哪部分知识库内容。"
    return answer


async def _stream_direct_path(
    orch: ChatOrchestrator,
    req: ChatRequest,
    decision,
    llm: LLMClient,
    text_id: str,
) -> AsyncIterator[str]:
    """DIRECT 路径 SSE 生成：LLM 完整调用 → 切片流式 → 流后 persist_turn。"""
    answer = _build_direct_answer(orch, req, decision, llm)

    yield _ai_sdk_sse(
        {"type": "start", "messageMetadata": {"session_id": req.session_id}}
    )
    yield _ai_sdk_sse({"type": "start-step"})
    yield _ai_sdk_sse({"type": "text-start", "id": text_id})
    for chunk in _text_chunks(answer):
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

    # 流结束后持久化（DIRECT 不写向量库）
    persist_turn(
        sessions=orch.sessions, memory=orch.memory, session_id=req.session_id,
        user_content=req.message, assistant_content=answer,
        intent="direct", index_vector=False,
    )
    maybe_generate_title(orch.sessions, llm, req.session_id, req.message)
    logger.info(
        "chat stream direct finish session=%s reason=%s",
        req.session_id, decision.reason,
    )


async def _stream_rag_unavailable_path(
    orch: ChatOrchestrator,
    req: ChatRequest,
    reason: str | None,
    llm: LLMClient,
    text_id: str,
) -> AsyncIterator[str]:
    """RAG 不可用路径 SSE 生成：把 hint 切片后流式输出，流后 persist_turn。"""
    hint = orch._RAG_UNAVAILABLE_HINTS.get(
        reason or "", "知识库当前不可用，请稍后重试。"
    )
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

    # 流结束后持久化（不写向量库）
    persist_turn(
        sessions=orch.sessions, memory=orch.memory, session_id=req.session_id,
        user_content=req.message, assistant_content=hint,
        intent="rag", index_vector=False,
    )
    maybe_generate_title(orch.sessions, llm, req.session_id, req.message)
    logger.info(
        "chat stream rag unavailable session=%s reason=%s", req.session_id, reason
    )


async def _stream_rag_path(
    orch: ChatOrchestrator,
    req: ChatRequest,
    decision,
    llm: LLMClient,
    text_id: str,
    *,
    transform_strategy,
) -> AsyncIterator[str]:
    """RAG 路径 SSE 生成：检索 + facts + 流式回答 + 持久化 + Inspector + finish。

    保留原内联实现的所有副作用顺序，仅切换持久化 API：
    - 流前：sessions.add_message(role="user", indexed=False)
    - 流后：sessions.add_message(role="assistant") + index_and_mark(...)
    """
    settings = orch.settings
    top_k = settings.top_k

    orch.kb_store.ensure_index()
    transform_degraded: str | None = None
    if decision.intent == ChatIntent.QUICK_RAG:
        sources = orch.kb_store.search(req.message, top_k=top_k)
    else:
        result = retrieve_with_transform(
            req.message, transform_strategy, orch.kb_store, settings, llm, top_k,
        )
        sources = result.chunks
        transform_degraded = result.degrade_reason if result.degraded else None

    memories = orch.memory.search(req.message, session_id=req.session_id, top_k=5)
    summary, recent = get_compacted_history(req.session_id, orch.sessions, llm)

    # 流前写 user 消息（与 sync _handle_rag 一致）
    user_msg = orch.sessions.add_message(
        session_id=req.session_id, role="user",
        content=req.message, intent=str(decision.intent), indexed=False,
    )

    # Session facts:active facts 拼成 prompt 片段 + 异步 fire-and-forget 提取。
    facts_store = SessionFactsStore(settings)
    facts_block = ""
    if settings.facts_enabled:
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
        settings=settings,
    )

    prompt_parts = [f"用户问题:\n{req.message}"]
    if facts_block:
        prompt_parts.append(facts_block)
    if summary:
        prompt_parts.append(f"会话历史摘要:\n{summary}")
    prompt_parts.append("最近会话:\n" + "\n".join(f"{m.role}: {m.content}" for m in recent))
    prompt_parts.append(f"记忆检索:\n{format_sources(memories)}")
    prompt_parts.append(f"知识库片段:\n{format_sources(sources)}")
    prompt_parts = _trim_to_budget(prompt_parts, settings.context_char_budget)
    system_prompt = "你是 Porto 知识库问答助手。优先基于知识库片段回答，也可引用会话记忆；不确定时说明缺口。"

    yield _ai_sdk_sse({"type": "start", "messageMetadata": {"session_id": req.session_id}})
    yield _ai_sdk_sse({"type": "start-step"})
    yield _ai_sdk_sse({"type": "text-start", "id": text_id})

    answer = ""
    streamed = settings.agent_stream_enabled and llm.enabled
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
                    f"- [{i + 1}] {s.path}: {s.text[:_SOURCE_PREVIEW_CHARS].replace(chr(10), ' ')}"
                    for i, s in enumerate(sources[:_MAX_FALLBACK_SOURCES])
                )
                answer = f"我在知识库中找到以下相关内容：\n{bullets}\n\n建议优先查看匹配分最高的文档并补充更具体的问题。"
            else:
                answer = "当前知识库没有检索到相关片段。请先执行知识库索引，或确认 `~/.scv/analysis` 中存在 md/txt/pdf/docx 文件。"
        for chunk in _text_chunks(answer):
            yield _ai_sdk_sse({"type": "text-delta", "id": text_id, "delta": chunk})

    yield _ai_sdk_sse({"type": "text-end", "id": text_id})

    # 流后：写 assistant 消息 + 索引 + 回填 flag
    asst_msg = orch.sessions.add_message(
        session_id=req.session_id, role="assistant",
        content=answer, intent=str(decision.intent), indexed=False,
    )
    index_and_mark(orch.sessions, orch.memory, [user_msg, asst_msg])
    maybe_generate_title(orch.sessions, llm, req.session_id, req.message)

    evaluation = evaluate_rag_cases(
        [EvalCase(question=req.message, answer=answer, contexts=[s.text for s in sources])]
    ).model_dump()
    for source in sources[:_MAX_SSE_SOURCES]:
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
    finish_meta: dict = {"evaluation": evaluation, "source_count": len(sources)}
    if transform_degraded is not None:
        finish_meta["transform_degraded"] = transform_degraded
    yield _ai_sdk_sse(
        {
            "type": "finish",
            "finishReason": "stop",
            "messageMetadata": finish_meta,
        }
    )
    yield "data: [DONE]\n\n"
    logger.info(
        "chat stream finish session_id=%s sources=%s streamed=%s degraded=%s",
        req.session_id,
        len(sources),
        streamed,
        transform_degraded,
    )


async def stream_chat(
    orch: ChatOrchestrator, req: ChatRequest, llm: LLMClient,
) -> AsyncIterator[str]:
    """主流式入口——在主函数内构造 ``decision`` / ``text_id``（这些
    必须在路由前一次性确定，避免重复触发 LLM 意图分类），然后委派给对应的
    子 async generator 完成实际 SSE 生成。

    ``llm`` 由 ChatOrchestrator.handle_stream 创建并传入（使 LLMClient patch
    在 orchestrator 模块即可覆盖 streaming 路径）。

    子 generator 内的异常通过 ``async for ... yield`` 委派自动传播到主函数的
    try/except，转为 SSE error + finish + [DONE] 三段式终止序列。
    """
    logger.info("chat stream start session_id=%s", req.session_id)

    decision = orch._route_intent(req, llm)
    text_id = "answer-1"

    try:
        if decision.intent == ChatIntent.DIRECT:
            async for chunk in _stream_direct_path(orch, req, decision, llm, text_id):
                yield chunk
            return

        available, reason = orch._check_rag_available()
        if not available:
            async for chunk in _stream_rag_unavailable_path(orch, req, reason, llm, text_id):
                yield chunk
            return

        async for chunk in _stream_rag_path(
            orch,
            req,
            decision,
            llm,
            text_id,
            transform_strategy=orch._transform_strategy,
        ):
            yield chunk
    except Exception as exc:
        logger.exception("chat stream failed session_id=%s", req.session_id)
        yield _ai_sdk_sse({"type": "error", "errorText": str(exc)})
        yield _ai_sdk_sse({"type": "finish", "finishReason": "error"})
        yield "data: [DONE]\n\n"
