"""确定性检索指标——零 LLM、秒级，用 golden 的 ``gold_doc_ids`` 直接计算。

这是双轨评估的「检索轨」：recall@k / MRR / hit@k / 噪音率全部是纯函数，
可在每次 PR 上做硬门禁（对照端到端轨的 LLM judge 指标，见 metrics.py）。

doc id 映射约定：provision 把 corpus doc 落盘为 ``{safe(doc_id)}.md``，索引后
chunk 的 ``path`` 形如 ``eval_kb/159.md``、``title`` 即 stem。本模块据此从
SourceChunk 还原来源 doc id，并通过 corpus 的 safe-name 映射表还原原始 id。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .provision import _safe_filename
from .schema import RagCorpus

# 检索轨批量均值门禁阈值（可被 RAG_EVAL_RECALL_MIN / RAG_EVAL_MRR_MIN /
# RAG_EVAL_HIT_MIN 覆盖）。按 2026-08-19 实测基线校准（baseline profile ×
# DomainRAG extractive 90 条：recall 0.72 / MRR 0.34 / hit 0.72），取
# 「均值 − 余量」防 flaky 并拦真实回归。
RETRIEVAL_THRESHOLDS: dict[str, float] = {
    "recall_at_k": float(os.environ.get("RAG_EVAL_RECALL_MIN", "0.55")),
    "mrr": float(os.environ.get("RAG_EVAL_MRR_MIN", "0.25")),
    "hit_at_k": float(os.environ.get("RAG_EVAL_HIT_MIN", "0.55")),
}

_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def doc_id_map(corpus: RagCorpus) -> dict[str, str]:
    """``safe(doc_id) → doc_id`` 映射（corpus 落盘文件名 → 原始 id）。"""
    return {_safe_filename(d.id): d.id for d in corpus.docs}


def retrieved_doc_ids(chunks: list[Any], id_map: dict[str, str] | None = None) -> list[str]:
    """从 SourceChunk 列表按序还原来源 doc id（path stem → 原始 id）。"""
    out: list[str] = []
    for c in chunks:
        stem = Path(str(getattr(c, "path", ""))).stem or str(getattr(c, "title", ""))
        out.append((id_map or {}).get(stem, stem))
    return out


# ────────────────────────────────────────────────────────────────────────── #
# 指标（全部纯函数，直接单测覆盖）
# ────────────────────────────────────────────────────────────────────────── #


def recall_at_k(gold_ids: list[str], retrieved_ids: list[str]) -> float:
    """|gold ∩ retrieved| / |gold|——gold 文档被检回的比例。"""
    if not gold_ids:
        return 0.0
    hit = len(set(gold_ids) & set(retrieved_ids))
    return round(hit / len(set(gold_ids)), 4)


def mrr(gold_ids: list[str], retrieved_ids: list[str]) -> float:
    """首个 gold 命中排名的倒数（未命中为 0）。"""
    gold = set(gold_ids)
    for rank, rid in enumerate(retrieved_ids, start=1):
        if rid in gold:
            return round(1.0 / rank, 4)
    return 0.0


def hit_at_k(gold_ids: list[str], retrieved_ids: list[str]) -> float:
    """任一 gold 命中即 1（二值），衡量「检得到」。"""
    if not gold_ids:
        return 0.0
    return 1.0 if set(gold_ids) & set(retrieved_ids) else 0.0


def noise_ratio(gold_ids: list[str], retrieved_ids: list[str]) -> float:
    """检索结果中非 gold 文档占比（噪音率，越低越好；只报告不门禁）。"""
    if not retrieved_ids:
        return 0.0
    gold = set(gold_ids)
    noise = sum(1 for rid in retrieved_ids if rid not in gold)
    return round(noise / len(retrieved_ids), 4)


def retrieval_case_metrics(gold_ids: list[str], retrieved_ids: list[str]) -> dict[str, float]:
    """单条 case 的全部确定性检索指标。"""
    return {
        "recall_at_k": recall_at_k(gold_ids, retrieved_ids),
        "mrr": mrr(gold_ids, retrieved_ids),
        "hit_at_k": hit_at_k(gold_ids, retrieved_ids),
        "noise_ratio": noise_ratio(gold_ids, retrieved_ids),
    }


def aggregate_retrieval(per_case: list[dict[str, float]]) -> dict[str, float]:
    """各检索指标跨 case 取均值（与 metrics.aggregate 同构）。"""
    keys = list(RETRIEVAL_THRESHOLDS) + ["noise_ratio"]
    if not per_case:
        return {k: 0.0 for k in keys}
    return {
        k: round(sum(c.get(k, 0.0) for c in per_case) / len(per_case), 4) for k in keys
    }


def judge_retrieval(per_metric_mean: dict[str, float]) -> tuple[bool, dict[str, dict]]:
    """对照 RETRIEVAL_THRESHOLDS：全指标达标才 pass（noise_ratio 仅报告）。"""
    detail: dict[str, dict] = {}
    for k, thr in RETRIEVAL_THRESHOLDS.items():
        mean = per_metric_mean.get(k, 0.0)
        detail[k] = {"mean": mean, "threshold": thr, "passed": mean >= thr}
    passed = all(d["passed"] for d in detail.values())
    return passed, detail
