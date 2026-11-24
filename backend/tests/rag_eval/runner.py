from __future__ import annotations

import time
from dataclasses import dataclass, field

from deepeval.test_case import LLMTestCase

from porto_chatbot.llm.client import LLMClient
from porto_chatbot.models.enums import QueryTransformStrategy
from porto_chatbot.query_transform import TransformResult, retrieve_with_transform

from .provision import EvalKb
from .schema import RagGolden

_RAG_SYSTEM_PROMPT = (
    "你是一个严格基于所提供上下文回答问题的助手。"
    "只使用上下文中的信息作答；若上下文不足以回答，请明确说明。回答用中文，简洁。"
)

_RAG_MULTITURN_SYSTEM_PROMPT = (
    "你是一个严格基于所提供上下文回答问题的多轮对话助手。"
    "对话历史仅供理解当前问题的指代与省略；回答只使用当前上下文中的信息，"
    "若上下文不足以回答，请明确说明。回答用中文，简洁。"
)


def run_rag(
    golden: RagGolden,
    eval_kb: EvalKb,
    llm: LLMClient,
    *,
    strategy: QueryTransformStrategy = QueryTransformStrategy.NONE,
    top_k: int = 6,
) -> tuple[LLMTestCase, TransformResult, dict]:
    """Retrieval (retrieve_with_transform) → generation (LLMClient.complete) → assemble the
    LLMTestCase.

    Returns ``(test_case, transform_result, timing)``; ``timing`` holds ``retrieve_s`` /
    ``generate_s`` / ``n_chunks`` for bottleneck diagnosis.
    """
    t0 = time.perf_counter()
    result = retrieve_with_transform(
        golden.question, strategy, eval_kb.store, eval_kb.settings, llm, top_k
    )
    t_retrieve = time.perf_counter() - t0

    if result.chunks:
        context = "\n\n".join(f"[{i}] {c.text}" for i, c in enumerate(result.chunks))
    else:
        context = "（无相关上下文）"

    t0 = time.perf_counter()
    answer = llm.complete(_RAG_SYSTEM_PROMPT, user=f"问题: {golden.question}\n\n上下文:\n{context}")
    t_generate = time.perf_counter() - t0
    answer = answer or "（模型未返回答案）"

    tc = LLMTestCase(
        input=golden.question,
        actual_output=answer,
        expected_output=golden.reference_answer,
        retrieval_context=[c.text for c in result.chunks],
    )
    timing = {
        "retrieve_s": round(t_retrieve, 2),
        "generate_s": round(t_generate, 2),
        "n_chunks": len(result.chunks),
    }
    return tc, result, timing


# ────────────────────────────────────────────────────────────────────────── #
# Multi-turn runner: the same "history goes into the prompt" family as the production
# orchestrator (an approximation: the vector memory / compaction / facts chain of the
# production ConversationMemory is outside this harness's scope, a gap Phase 3 "online sampled
# review" is meant to close). Each turn goes through retrieve_with_transform on its own, so the
# real retrieval behavior of coreference / ellipsis questions without history-based rewriting
# is measured faithfully.
# ────────────────────────────────────────────────────────────────────────── #


@dataclass
class TurnRecord:
    """Raw record of one turn's retrieval and generation (consumed by the deterministic retrieval
    metrics and by attribution)."""

    question: str
    answer: str
    chunks: list = field(default_factory=list)
    timing: dict = field(default_factory=dict)
    condensed: str = ""  # the actual retrieval query after the condense rewrite (empty when not rewritten)


@dataclass
class MultiturnResult:
    turns: list[TurnRecord] = field(default_factory=list)
    test_case: LLMTestCase | None = None  # last turn (the object the judge scores)
    transform_result: TransformResult | None = None  # last turn
    timing: dict = field(default_factory=dict)


def run_rag_multiturn(
    golden: RagGolden,
    eval_kb: EvalKb,
    llm: LLMClient,
    *,
    strategy: QueryTransformStrategy = QueryTransformStrategy.NONE,
    top_k: int = 6,
    history_turns: int = 4,
) -> MultiturnResult:
    """Retrieve + generate turn by turn with the conversation history injected into the prompt;
    **condense gating goes through the production node**: when
    ``eval_kb.settings.condense_enabled`` (injected from profile.condense_history by provision)
    is on, ``retrieve_with_transform`` first decides whether coreference resolution is needed.
    """
    assert golden.turns, "多轮 runner 需要 golden.turns"
    history: list[tuple[str, str]] = []
    out = MultiturnResult()
    total_retrieve = total_generate = 0.0

    for t in golden.turns:
        t0 = time.perf_counter()
        result = retrieve_with_transform(
            t.question, strategy, eval_kb.store, eval_kb.settings, llm, top_k,
            history=history[-history_turns:] or None,
        )
        t_retrieve = time.perf_counter() - t0
        condensed = result.condensed_query

        context = (
            "\n\n".join(f"[{i}] {c.text}" for i, c in enumerate(result.chunks))
            if result.chunks
            else "（无相关上下文）"
        )
        history_text = "\n".join(
            f"用户: {q}\n助手: {a[:200]}" for q, a in history[-history_turns:]
        )
        user_msg = (
            (f"对话历史:\n{history_text}\n\n" if history_text else "")
            + f"当前问题: {t.question}\n\n上下文:\n{context}"
        )

        t0 = time.perf_counter()
        answer = llm.complete(_RAG_MULTITURN_SYSTEM_PROMPT, user=user_msg)
        t_generate = time.perf_counter() - t0
        answer = answer or "（模型未返回答案）"

        history.append((t.question, answer))
        total_retrieve += t_retrieve
        total_generate += t_generate
        out.turns.append(
            TurnRecord(
                question=t.question,
                answer=answer,
                chunks=list(result.chunks),
                condensed=condensed,
                timing={
                    "retrieve_s": round(t_retrieve, 2),
                    "generate_s": round(t_generate, 2),
                    "n_chunks": len(result.chunks),
                },
            )
        )
        final_result, final_chunks, final_answer = result, result.chunks, answer

    out.transform_result = final_result
    out.test_case = LLMTestCase(
        input=golden.turns[-1].question,
        actual_output=final_answer,
        expected_output=golden.turns[-1].reference_answer,
        retrieval_context=[c.text for c in final_chunks],
    )
    n = len(out.turns)
    out.timing = {
        "n_turns": n,
        "retrieve_s_mean": round(total_retrieve / n, 2),
        "generate_s_mean": round(total_generate / n, 2),
    }
    return out
