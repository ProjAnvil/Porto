from __future__ import annotations

import time

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
