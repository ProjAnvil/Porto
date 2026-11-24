"""Deterministic retrieval metrics: no LLM, seconds to run, computed directly from the
golden ``gold_doc_ids``.

This is the retrieval track of the dual-track evaluation: recall@k / MRR / hit@k / noise
ratio are all pure functions, so they can be a hard gate on every PR (unlike the LLM-judge
metrics of the end-to-end track, see metrics.py).

Doc id mapping convention: provision writes each corpus doc to ``{safe(doc_id)}.md``;
after indexing a chunk's ``path`` looks like ``eval_kb/159.md`` and its ``title`` is the
stem. This module recovers the source doc id from a SourceChunk that way, then maps the
safe name back to the original id through the corpus.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .provision import _safe_filename
from .schema import RagCorpus

# Gate thresholds on batch means for the retrieval track (override with
# RAG_EVAL_RECALL_MIN / RAG_EVAL_MRR_MIN / RAG_EVAL_HIT_MIN). Calibrated on the measured
# 2026-08-19 baseline (baseline profile × DomainRAG extractive, 90 cases: recall 0.72 /
# MRR 0.34 / hit 0.72), set to "mean − margin" to avoid flakes while still catching real
# regressions.
RETRIEVAL_THRESHOLDS: dict[str, float] = {
    "recall_at_k": float(os.environ.get("RAG_EVAL_RECALL_MIN", "0.55")),
    "mrr": float(os.environ.get("RAG_EVAL_MRR_MIN", "0.25")),
    "hit_at_k": float(os.environ.get("RAG_EVAL_HIT_MIN", "0.55")),
}

_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def doc_id_map(corpus: RagCorpus) -> dict[str, str]:
    """``safe(doc_id) → doc_id`` mapping (on-disk corpus file name → original id)."""
    return {_safe_filename(d.id): d.id for d in corpus.docs}


def retrieved_doc_ids(chunks: list[Any], id_map: dict[str, str] | None = None) -> list[str]:
    """Recover the source doc ids, in order, from a list of SourceChunks (path stem → original
    id)."""
    out: list[str] = []
    for c in chunks:
        stem = Path(str(getattr(c, "path", ""))).stem or str(getattr(c, "title", ""))
        out.append((id_map or {}).get(stem, stem))
    return out


# ────────────────────────────────────────────────────────────────────────── #
# Metrics (all pure functions, covered directly by unit tests)
# ────────────────────────────────────────────────────────────────────────── #


def recall_at_k(gold_ids: list[str], retrieved_ids: list[str]) -> float:
    """|gold ∩ retrieved| / |gold|: the share of gold documents that were retrieved."""
    if not gold_ids:
        return 0.0
    hit = len(set(gold_ids) & set(retrieved_ids))
    return round(hit / len(set(gold_ids)), 4)


def mrr(gold_ids: list[str], retrieved_ids: list[str]) -> float:
    """Reciprocal rank of the first gold hit (0 when nothing hits)."""
    gold = set(gold_ids)
    for rank, rid in enumerate(retrieved_ids, start=1):
        if rid in gold:
            return round(1.0 / rank, 4)
    return 0.0


def hit_at_k(gold_ids: list[str], retrieved_ids: list[str]) -> float:
    """1 if any gold document is retrieved (binary); measures "can it be found at all"."""
    if not gold_ids:
        return 0.0
    return 1.0 if set(gold_ids) & set(retrieved_ids) else 0.0


def noise_ratio(gold_ids: list[str], retrieved_ids: list[str]) -> float:
    """Share of retrieved documents that are not gold (noise ratio; lower is better, reported
    only and not gated)."""
    if not retrieved_ids:
        return 0.0
    gold = set(gold_ids)
    noise = sum(1 for rid in retrieved_ids if rid not in gold)
    return round(noise / len(retrieved_ids), 4)


def retrieval_case_metrics(gold_ids: list[str], retrieved_ids: list[str]) -> dict[str, float]:
    """All deterministic retrieval metrics for one case."""
    return {
        "recall_at_k": recall_at_k(gold_ids, retrieved_ids),
        "mrr": mrr(gold_ids, retrieved_ids),
        "hit_at_k": hit_at_k(gold_ids, retrieved_ids),
        "noise_ratio": noise_ratio(gold_ids, retrieved_ids),
    }


def aggregate_retrieval(per_case: list[dict[str, float]]) -> dict[str, float]:
    """Mean of each retrieval metric across cases (same shape as metrics.aggregate)."""
    keys = list(RETRIEVAL_THRESHOLDS) + ["noise_ratio"]
    if not per_case:
        return {k: 0.0 for k in keys}
    return {
        k: round(sum(c.get(k, 0.0) for c in per_case) / len(per_case), 4) for k in keys
    }


def judge_retrieval(per_metric_mean: dict[str, float]) -> tuple[bool, dict[str, dict]]:
    """Compare against RETRIEVAL_THRESHOLDS: pass only if every metric clears its threshold
    (noise_ratio is reported only)."""
    detail: dict[str, dict] = {}
    for k, thr in RETRIEVAL_THRESHOLDS.items():
        mean = per_metric_mean.get(k, 0.0)
        detail[k] = {"mean": mean, "threshold": thr, "passed": mean >= thr}
    passed = all(d["passed"] for d in detail.values())
    return passed, detail
