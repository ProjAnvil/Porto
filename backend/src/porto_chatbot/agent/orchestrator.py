"""ChatOrchestrator — langchain chat 路径的编排层。

从 langchain_chat.py 提取核心 chat 逻辑：intent routing → RAG availability →
retrieval → compaction → facts → prompt → LLM → 持久化 → 评估。

用 SessionStore + ConversationMemory 替代旧 MemoryStore，实现：
- DIRECT 消息写 SQLite 不写向量库
- RAG 消息写 SQLite + 写向量库 + 回填 indexed flag
- Session 一等实体（自动 ensure + 标题生成）
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ..api.deps import effective_rag_chat_settings, get_index_supervisor
from ..api.sse import _ai_sdk_sse, _text_chunks
from ..evaluation import evaluate_rag_cases
from ..intent import IntentDecision, route_chat_intent
from ..llm import LLMClient, format_sources
from ..logging_utils import get_component_logger
from ..memory import (
    SessionFactsStore,
    build_facts_prompt,
    get_compacted_history,
    index_and_mark,
    maybe_generate_title,
    persist_turn,
    trigger_facts_extraction_async,
    trigger_facts_extraction_sync,
)
from ..models import ChatRequest, ChatResponse, EvalCase, MessageRecord
from ..models.enums import ChatIntent, IntentRoutingMode, QueryTransformStrategy
from ..query_transform import retrieve_with_transform
from ..vector_store import LocalVectorStore

logger = get_component_logger("orchestrator")

_SOURCE_PREVIEW_CHARS = 180
_MAX_FALLBACK_SOURCES = 4
_MAX_SSE_SOURCES = 6


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


class ChatOrchestrator:
    """编排 langchain chat 流程。

    sync: handle() → ChatResponse
    async: handle_stream() → AsyncIterator[str] (SSE)
    """

    def __init__(
        self,
        sessions,  # SessionStore
        memory,    # ConversationMemory
        kb_store: LocalVectorStore,
        settings,
    ):
        self.sessions = sessions
        self.memory = memory
        self.kb_store = kb_store
        self.settings = settings

    # ── 路由辅助 ──

    def _route_intent(self, req: ChatRequest, llm: LLMClient) -> IntentDecision:
        rag_chat = effective_rag_chat_settings()
        routing_mode = rag_chat.intent_routing_mode or IntentRoutingMode.BINARY
        if routing_mode == IntentRoutingMode.OFF:
            return IntentDecision(ChatIntent.RAG, "routing_off")
        return route_chat_intent(req.message, self.settings, llm, routing_mode=routing_mode)

    @property
    def _transform_strategy(self) -> QueryTransformStrategy:
        rag_chat = effective_rag_chat_settings()
        return rag_chat.query_transform_strategy or QueryTransformStrategy.NONE

    def _check_rag_available(self) -> tuple[bool, str | None]:
        return get_index_supervisor().rag_available()

    def _llm_complete(self, system: str, user: str, llm: LLMClient | None = None) -> str:
        llm = llm or LLMClient(self.settings)
        return llm.complete(system, user)

    # ── 主入口 ──

    def handle(self, req: ChatRequest) -> ChatResponse:
        """sync chat 入口——intent routing → dispatch → persist → evaluate。"""
        logger.info(
            "chat start session=%s chars=%s", req.session_id, len(req.message),
        )
        llm = LLMClient(self.settings)
        decision = self._route_intent(req, llm)

        if decision.intent == ChatIntent.DIRECT:
            return self._handle_direct(req, decision, llm)

        available, reason = self._check_rag_available()
        if not available:
            return self._handle_rag_unavailable(req, reason, decision, llm)

        return self._handle_rag(req, decision, llm)

    # ── DIRECT 路径 ──

    def _handle_direct(
        self, req: ChatRequest, decision: IntentDecision, llm: LLMClient,
    ) -> ChatResponse:
        answer = self._llm_complete(
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

        persist_turn(
            sessions=self.sessions, memory=self.memory, session_id=req.session_id,
            user_content=req.message, assistant_content=answer,
            intent="direct", index_vector=False,
        )
        maybe_generate_title(self.sessions, llm, req.session_id, req.message)

        logger.info("chat direct finish session=%s reason=%s", req.session_id, decision.reason)
        return ChatResponse(
            answer=answer, sources=[], memory=[],
            evaluation={"score": 0.0, "passed": True, "cases": []},
            steps=[
                {"name": "route_intent", "status": "completed",
                 "summary": f"direct: {decision.reason}",
                 "data": {"intent": decision.intent, "reason": decision.reason}},
                {"name": "answer", "status": "completed",
                 "summary": "直接回复，未调用 RAG", "data": {}},
            ],
        )

    # ── RAG 不可用 ──

    _RAG_UNAVAILABLE_HINTS = {
        "reindexing": "知识库正在重建索引，请等待完成后再提问。",
        "index_unavailable": "知识库索引不可用，请在设置中触发重新索引后再提问。",
    }

    def _handle_rag_unavailable(
        self, req: ChatRequest, reason: str | None,
        decision: IntentDecision, llm: LLMClient,
    ) -> ChatResponse:
        hint = self._RAG_UNAVAILABLE_HINTS.get(reason or "", "知识库当前不可用，请稍后重试。")
        # Persist user message + hint as a turn (index_vector=False)
        persist_turn(
            sessions=self.sessions, memory=self.memory, session_id=req.session_id,
            user_content=req.message, assistant_content=hint,
            intent="rag", index_vector=False,
        )
        maybe_generate_title(self.sessions, llm, req.session_id, req.message)
        return ChatResponse(
            answer=hint, sources=[], memory=[],
            evaluation={"score": 0.0, "passed": False, "cases": []},
            steps=[
                {"name": "route_intent", "status": "completed",
                 "summary": f"rag unavailable: {reason}",
                 "data": {"reason": reason}},
                {"name": "retrieve_knowledge", "status": "completed",
                 "summary": hint, "data": {}},
                {"name": "answer", "status": "completed",
                 "summary": "RAG 不可用，返回提示", "data": {}},
            ],
        )

    # ── RAG 路径 ──

    def _handle_rag(
        self, req: ChatRequest, decision: IntentDecision, llm: LLMClient,
    ) -> ChatResponse:
        top_k = self.settings.top_k
        transform_strategy = self._transform_strategy
        transform_degraded: str | None = None

        self.kb_store.ensure_index()
        if decision.intent == ChatIntent.QUICK_RAG:
            sources = self.kb_store.search(req.message, top_k=top_k)
        else:
            result = retrieve_with_transform(
                req.message, transform_strategy, self.kb_store, self.settings, llm, top_k,
            )
            sources = result.chunks
            transform_degraded = result.degrade_reason if result.degraded else None

        memories = self.memory.search(req.message, session_id=req.session_id, top_k=5)
        summary, recent = get_compacted_history(req.session_id, self.sessions, llm)

        # Write user message BEFORE LLM (for session history + future compaction)
        user_msg = self.sessions.add_message(
            session_id=req.session_id, role="user",
            content=req.message, intent=str(decision.intent), indexed=False,
        )

        # Facts
        facts_store = SessionFactsStore(self.settings)
        facts_block = ""
        if self.settings.facts_enabled:
            try:
                facts_block = build_facts_prompt(facts_store.by_category(req.session_id))
            except Exception:
                logger.exception("facts load failed session=%s", req.session_id)
        trigger_facts_extraction_sync(
            store=facts_store, llm=llm, session_id=req.session_id,
            new_message=req.message, recent_turns=recent, settings=self.settings,
        )

        # Prompt assembly
        prompt_parts = [f"用户问题:\n{req.message}"]
        if facts_block:
            prompt_parts.append(facts_block)
        if summary:
            prompt_parts.append(f"会话历史摘要:\n{summary}")
        prompt_parts.append("最近会话:\n" + "\n".join(f"{m.role}: {m.content}" for m in recent))
        prompt_parts.append(f"记忆检索:\n{format_sources(memories)}")
        prompt_parts.append(f"知识库片段:\n{format_sources(sources)}")
        prompt_parts = _trim_to_budget(prompt_parts, self.settings.context_char_budget)

        answer = self._llm_complete(
            "你是 Porto 知识库问答助手。优先基于知识库片段回答，也可引用会话记忆；不确定时说明缺口。",
            "\n\n".join(prompt_parts), llm,
        )
        if not answer:
            if sources:
                bullets = "\n".join(
                    f"- [{i + 1}] {s.path}: {s.text[:_SOURCE_PREVIEW_CHARS].replace(chr(10), ' ')}"
                    for i, s in enumerate(sources[:_MAX_FALLBACK_SOURCES])
                )
                answer = f"我在知识库中找到以下相关内容：\n{bullets}\n\n建议优先查看匹配分最高的文档。"
            else:
                answer = "当前知识库没有检索到相关片段。请先执行知识库索引。"

        # Write assistant message + index + mark
        asst_msg = self.sessions.add_message(
            session_id=req.session_id, role="assistant",
            content=answer, intent=str(decision.intent), indexed=False,
        )
        index_and_mark(self.sessions, self.memory, [user_msg, asst_msg])
        maybe_generate_title(self.sessions, llm, req.session_id, req.message)

        evaluation = evaluate_rag_cases([
            EvalCase(question=req.message, answer=answer,
                     contexts=[s.text for s in sources]),
        ]).model_dump()

        logger.info(
            "chat rag finish session=%s sources=%s memories=%s score=%s",
            req.session_id, len(sources), len(memories), evaluation["score"],
        )
        return ChatResponse(
            answer=answer, sources=sources, memory=memories,
            evaluation=evaluation, transform_degraded=transform_degraded,
            steps=[
                {"name": "route_intent", "status": "completed",
                 "summary": f"rag: {decision.reason}",
                 "data": {"intent": decision.intent, "reason": decision.reason}},
                {"name": "retrieve_memory", "status": "completed",
                 "summary": f"检索到 {len(memories)} 条记忆，近期 {len(recent)} 条"
                            + ("（含历史摘要）" if summary else ""),
                 "data": {"compacted": bool(summary), "recent": len(recent),
                          "memory_hits": len(memories)}},
                {"name": "retrieve_knowledge", "status": "completed",
                 "summary": f"检索到 {len(sources)} 个片段", "data": {}},
                {"name": "answer", "status": "completed",
                 "summary": "完成回答生成", "data": {}},
                {"name": "evaluate_rag", "status": "completed",
                 "summary": f"RAG eval score {evaluation['score']}", "data": evaluation},
            ],
        )

    # ── 流式入口 ──

    async def handle_stream(self, req: ChatRequest) -> AsyncIterator[str]:
        """async chat 流式入口——yield SSE chunks。

        保留 langchain_chat_stream 的 SSE 协议。
        """
        # 委托给模块级流式函数（从 langchain_chat.py 搬入）。
        # LLM 在这里创建（而非 stream_chat 内部），使 LLMClient patch 在
        # orchestrator 模块即可同时覆盖 sync + streaming 路径。
        from .langchain_chat_stream import stream_chat

        llm = LLMClient(self.settings)
        async for chunk in stream_chat(self, req, llm):
            yield chunk
