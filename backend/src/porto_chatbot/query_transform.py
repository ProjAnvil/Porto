from __future__ import annotations

from dataclasses import dataclass

from .logging_utils import get_component_logger
from .models import SourceChunk
from .models.enums import QueryTransformStrategy
from .retrieval import rerank_chunks
from .settings import Settings

logger = get_component_logger("query_transform")


@dataclass
class TransformResult:
    """retrieve_with_transform 的返回：检索结果 + 降级标记。

    Attributes
    ----------
    chunks:
        最终返回给上层的 chunk 列表（已按策略变换/融合，并按需 rerank）。
    degraded:
        True 表示 LLM 调用失败、已 fail-open 回退到 ``store._search_raw``。
        NONE 策略恒为 False（与 today 行为完全一致）。
    degrade_reason:
        降级原因的短字符串（如 ``"llm_call_failed"``）；未降级时为空串。
    """

    chunks: list[SourceChunk]
    degraded: bool = False
    degrade_reason: str = ""


def _rrf_fuse(rankings: list[list[SourceChunk]], k: int = 60) -> list[SourceChunk]:
    """Reciprocal Rank Fusion：多组 ranking 按 ``1/(rank+k)`` 求和合并。

    每个 chunk 的最终分数 = Σ 1/(rank_i + k)，按合并分数降序返回。同 chunk 在
    多组 ranking 中出现时取首次出现的实例，分数更新为融合后的 RRF 分数
    （``round(..., 4)``）。

    Parameters
    ----------
    rankings:
        多组 ``store._search_raw`` 的结果列表（已按相关度排序）。
    k:
        RRF 平滑常数，标准取 60。
    """

    scores: dict[str, float] = {}
    best: dict[str, SourceChunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking):
            scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (rank + k)
            best.setdefault(chunk.id, chunk)
    ordered = sorted(scores, key=lambda cid: scores[cid], reverse=True)
    # SourceChunk 是 pydantic BaseModel —— 用 model_copy(update=...) 重建而非 dict-spread
    return [best[cid].model_copy(update={"score": round(scores[cid], 4)}) for cid in ordered]


def _merge_dedupe(rankings: list[list[SourceChunk]]) -> list[SourceChunk]:
    """子问题结果按出现顺序合并去重（首个出现的位置保留，score 不变）。"""

    seen: set[str] = set()
    out: list[SourceChunk] = []
    for ranking in rankings:
        for chunk in ranking:
            if chunk.id not in seen:
                seen.add(chunk.id)
                out.append(chunk)
    return out


def retrieve_with_transform(
    query: str,
    strategy: QueryTransformStrategy,
    store,
    settings: Settings,
    llm,
    top_k: int,
) -> TransformResult:
    """按 ``strategy`` 改写 query 后做基础检索，必要时融合 + rerank。

    合约
    -----
    - ``strategy == NONE``：直接调用 ``store.search(query, top_k)``，**与 today
      行为零变化**（含其内部 rerank），返回 ``degraded=False``。
    - 其他策略：LLM 生成改写 query → ``store._search_raw`` 检索 → 按策略融合；
      LLM 异常 fail-open 回退到 ``store._search_raw(query, top_k)``，并返回
      ``TransformResult(degraded=True, degrade_reason="llm_call_failed")``。
      **绝不向上抛**——chat 链路永不因此崩溃。
    - rerank 作为最后一步：仅在 ``settings.rerank_enabled`` 且非 NONE 路径时执行
      （NONE 路径的 rerank 由 ``store.search`` 内部负责，避免双次 rerank）。
    """
    if strategy == QueryTransformStrategy.NONE:
        # 零行为变化：完全走 store.search 原路径（含其内部 rerank 逻辑）
        return TransformResult(store.search(query, top_k))

    try:
        if strategy == QueryTransformStrategy.HYDE:
            fake_doc = _generate_hypothetical(llm, query)
            rows = store._search_raw(fake_doc, top_k)
        elif strategy == QueryTransformStrategy.MULTI_QUERY:
            variants = _generate_query_variants(llm, query, settings.multi_query_count)
            rows = _rrf_fuse([store._search_raw(v, top_k) for v in variants])
        elif strategy == QueryTransformStrategy.DECOMPOSITION:
            sub_qs = _decompose(llm, query)
            rows = _merge_dedupe([store._search_raw(s, top_k) for s in sub_qs])
        elif strategy == QueryTransformStrategy.STEP_BACK:
            abstract = _step_back(llm, query)
            rows = store._search_raw(abstract, top_k)
        else:  # 防御性兜底（新策略未接入时按原 query 做基础检索）
            rows = store._search_raw(query, top_k)
    except Exception:
        # fail-open：任何异常（LLM 调用、解析、NotImplementedError 桩）均回退
        logger.exception("transform failed strategy=%s → fallback raw", strategy)
        rows = store._search_raw(query, top_k)
        return TransformResult(rows, degraded=True, degrade_reason="llm_call_failed")

    if settings.rerank_enabled:
        rows = rerank_chunks(rows, query, settings)
    return TransformResult(rows)


# --- 各策略 LLM 生成函数（Task 6 实现） ---
def _generate_hypothetical(llm, query: str) -> str:
    """HYDE：让 LLM 生成假设答案，用作检索 query。Task 6 实现。"""
    raise NotImplementedError  # Task 6


def _generate_query_variants(llm, query: str, n: int) -> list[str]:
    """MULTI_QUERY：让 LLM 生成 n 条改写变体。Task 6 实现。"""
    raise NotImplementedError  # Task 6


def _decompose(llm, query: str) -> list[str]:
    """DECOMPOSITION：让 LLM 拆解子问题。Task 6 实现。"""
    raise NotImplementedError  # Task 6


def _step_back(llm, query: str) -> str:
    """STEP_BACK：让 LLM 生成更高阶的抽象问题。Task 6 实现。"""
    raise NotImplementedError  # Task 6
