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
    """retrieve_with_transform 的返回：检索结果 + 降级/改写标记。

    Attributes
    ----------
    chunks:
        最终返回给上层的 chunk 列表（已按策略变换/融合，并按需 rerank）。
    degraded:
        True 表示 LLM 调用失败、已 fail-open 回退到 ``store._search_raw``。
        NONE 策略恒为 False（与 today 行为完全一致）。
    degrade_reason:
        降级原因的短字符串（如 ``"llm_call_failed"``）；未降级时为空串。
    condense_applied:
        condense 门控节点判定「需要指代消解」且已改写检索查询。
    condensed_query:
        condense 改写后的实际检索查询（未改写时为空串）。
    """

    chunks: list[SourceChunk]
    degraded: bool = False
    degrade_reason: str = ""
    condense_applied: bool = False
    condensed_query: str = ""


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
    history: list[tuple[str, str]] | None = None,
) -> TransformResult:
    """condense 门控（可选）→ 按 ``strategy`` 改写 query → 检索 → 融合 + rerank。

    合约
    -----
    - **condense 门控节点（管线第一段，可选）**：传入 ``history`` 且
      ``settings.condense_enabled`` 时，先由 ``condense_with_gate`` 判断——
      问题已可独立检索则**直通不改写**（零额外行为），含指代/省略才改写为
      完整查询再进入后续策略/检索。门控 fail-open，永不阻塞检索。
    - ``strategy == NONE``：直接调用 ``store.search(query, top_k)``，**与 today
      行为零变化**（含其内部 rerank），返回 ``degraded=False``。
    - 其他策略：LLM 生成改写 query → ``store._search_raw`` 检索 → 按策略融合；
      LLM 异常 fail-open 回退到 ``store._search_raw(query, top_k)``，并返回
      ``TransformResult(degraded=True, degrade_reason="llm_call_failed")``。
      **绝不向上抛**——chat 链路永不因此崩溃。
    - rerank 作为最后一步：仅在 ``settings.rerank_enabled`` 且非 NONE 路径时执行
      （NONE 路径的 rerank 由 ``store.search`` 内部负责，避免双次 rerank）。
    """
    # ── condense 门控：检索前的指代消解（可独立于任意 strategy 组合）──
    # 与策略族同样 fail-open：门控任何异常（LLM 网络/解析）都直通原查询，绝不阻塞检索
    condense_applied, condensed = False, ""
    if history and getattr(settings, "condense_enabled", False):
        try:
            gate = condense_with_gate(llm, query, history)
            condense_applied = gate.needed
            if gate.needed:
                query, condensed = gate.query, gate.query
        except Exception:
            logger.exception("condense gate failed → pass-through original query")

    if strategy == QueryTransformStrategy.NONE:
        # 零行为变化：完全走 store.search 原路径（含其内部 rerank 逻辑）
        return TransformResult(
            store.search(query, top_k),
            condense_applied=condense_applied,
            condensed_query=condensed,
        )

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
        return TransformResult(
            rows, degraded=True, degrade_reason="llm_call_failed",
            condense_applied=condense_applied, condensed_query=condensed,
        )

    if settings.rerank_enabled:
        rows = rerank_chunks(rows, query, settings)
    return TransformResult(
        rows, condense_applied=condense_applied, condensed_query=condensed,
    )


# --- condense 门控节点（检索前指代消解）+ 各策略 LLM 生成函数（Task 6 实现）--- #

_PASS_TOKEN = "PASS"


@dataclass
class CondenseResult:
    """condense 门控节点的判定结果。

    ``needed=False`` 表示问题已可独立检索（无历史 / LLM 判定无需改写 / 失败直通），
    ``query`` 即原始问题——直接放行，不改写。
    """

    needed: bool
    query: str
    reason: str = ""


def condense_with_gate(llm, question: str, history: list[tuple[str, str]]) -> CondenseResult:
    """多轮指代消解**门控节点**：LLM 先判断是否需要，需要才改写，否则直通。

    检索前优化手段之一：把「它成立于哪一年」这类含指代/省略的问题改写为
    独立完整查询（还原代词与省略成分、保留实体全称），再交给检索/策略。

    判定与改写合并为一次 LLM 调用（输出协议：已完整 → 只输出 ``PASS``；
    需要改写 → 输出改写后的查询本身），避免「判断 + 改写」两次调用的延迟。

    分级直通（零成本放行）：
    1. 无对话历史 → 问题必然可独立检索，不调 LLM；
    2. LLM 不可用/返回空 → fail-open 返回原问题（与策略族一致，绝不向上抛）。
    """
    if not history or llm is None:
        return CondenseResult(False, question, reason="no_history_or_no_llm")
    history_text = "\n".join(f"用户: {q}\n助手: {a[:200]}" for q, a in history[-6:])
    out = llm.complete(
        "判断「当前问题」脱离对话历史后是否仍能独立用于知识库检索——"
        "若含指代代词（它/他/该/上述…）、省略主语或依赖前文才能确定检索对象，"
        "把它改写为一个独立、完整、无指代的中文检索查询（还原代词与省略成分、"
        "保留实体全称，不要添加原文没有的信息）；若问题已完整，只输出 PASS。"
        "只输出 PASS 或改写后的查询本身，不要任何解释。",
        f"对话历史:\n{history_text}\n\n当前问题: {question}",
    )
    cleaned = (out or "").strip().strip('"').strip("\u201c\u201d").strip("'")
    if not cleaned:
        return CondenseResult(False, question, reason="llm_empty_failopen")
    if cleaned.upper().rstrip("。.!！") == _PASS_TOKEN or cleaned == question:
        return CondenseResult(False, question, reason="already_standalone")
    return CondenseResult(True, cleaned)


def condense_question(llm, question: str, history: list[tuple[str, str]]) -> str:
    """兼容包装：只要改写后的查询（等价 ``condense_with_gate(...).query``）。"""
    return condense_with_gate(llm, question, history).query


def _generate_hypothetical(llm, query: str) -> str:
    """HyDE：让 LLM 生成一个假设性答案文档，用它（而非原问题）去检索。"""
    return llm.complete(
        "请根据用户问题，写一段假设性的、像知识库文档一样的答案（中文，3-5 句）。"
        "只输出答案正文，不要解释、不要说'假设'。",
        f"用户问题: {query}",
    ).strip()


def _generate_query_variants(llm, query: str, n: int) -> list[str]:
    """Multi-Query：生成 n 个改写 query，每行一个。"""
    raw = llm.complete(
        f"请把下面的问题改写成 {n} 个语义相同但表述不同的检索用查询（每行一个，不要编号）：",
        f"原问题: {query}",
    )
    variants = [line.strip() for line in raw.splitlines() if line.strip()]
    return variants[:n] if variants else [query]


def _decompose(llm, query: str) -> list[str]:
    """Decomposition：把复杂问题拆成子问题，每行一个。"""
    raw = llm.complete(
        "请把下面的问题拆成若干个独立的子问题（每行一个，不要编号），便于分别检索：",
        f"原问题: {query}",
    )
    subs = [line.strip() for line in raw.splitlines() if line.strip()]
    return subs if subs else [query]


def _step_back(llm, query: str) -> str:
    """Step-Back：抽象出更高层的背景问题。"""
    return llm.complete(
        "请把下面的问题抽象成一个更宽泛的背景问题（一句话），用于检索相关背景知识。只输出问题本身：",
        f"原问题: {query}",
    ).strip()
