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
    """检索（retrieve_with_transform）→ 生成（LLMClient.complete）→ 组装 LLMTestCase。

    返回 ``(test_case, transform_result, timing)``；``timing`` 含 ``retrieve_s`` /
    ``generate_s`` / ``n_chunks``，用于瓶颈诊断。
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
# 多轮 runner：与生产 orchestrator 同族的「历史进 prompt」模式（近似实现——
# 生产 ConversationMemory 的向量记忆/压缩/facts 链路不在本 harness 覆盖范围内，
# 属 Phase 3「在线抽样复审」要补的缺口）。逐轮独立走 retrieve_with_transform，
# 指代/省略问题在无历史改写下的真实检索表现因此被如实测量。
# ────────────────────────────────────────────────────────────────────────── #


@dataclass
class TurnRecord:
    """单轮的检索与生成原始记录（供确定性检索指标与归因消费）。"""

    question: str
    answer: str
    chunks: list = field(default_factory=list)
    timing: dict = field(default_factory=dict)
    condensed: str = ""  # condense 改写后的实际检索查询（未改写时为空）


@dataclass
class MultiturnResult:
    turns: list[TurnRecord] = field(default_factory=list)
    test_case: LLMTestCase | None = None  # 末轮（被 judge 的对象）
    transform_result: TransformResult | None = None  # 末轮
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
    """逐轮检索+生成，对话历史注入 prompt；**condense 门控走生产节点**——
    ``eval_kb.settings.condense_enabled``（由 profile.condense_history 经
    provision 注入）开启时，``retrieve_with_transform`` 内部先做指代消解判断。
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
