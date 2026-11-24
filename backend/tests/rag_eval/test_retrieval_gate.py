"""Retrieval-track hard gate: deterministic retrieval metrics (recall@k / MRR / hit@k)
plus retrieval latency p95.

The PR gate of the dual-track evaluation: no judge LLM; with the baseline profile (local
embedding, NONE strategy, no rerank) it is **entirely LLM-free** and must run without any
key. Retrieval regressions are caught here.

Dataset: DomainRAG extractive by default (when the data is missing it falls back to the
synth smoke set and degrades to report-only, so a CI run without the cache is neither
falsely green nor falsely red). env::

    RAG_EVAL_RETRIEVAL_PROFILE=smallchunk pytest ...  # default: baseline (zero LLM)
    RAG_EVAL_RETRIEVAL_DATASET=domainrag:multidoc pytest
    RAG_EVAL_RECALL_MIN=0.5 pytest ...                # override a threshold (metrics / retrieval_metrics read it)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from .experiment import run_experiment
from .loaders.domainrag import DATA_DIR
from .profiles import get_profile

pytestmark = pytest.mark.integration


def test_retrieval_gate():
    profile = get_profile(os.environ.get("RAG_EVAL_RETRIEVAL_PROFILE", "baseline"))
    if max_cases := os.environ.get("RAG_EVAL_RETRIEVAL_MAX_CASES"):
        profile.max_cases = int(max_cases)
    dataset = os.environ.get("RAG_EVAL_RETRIEVAL_DATASET", "domainrag")

    report_only = False
    if not (DATA_DIR / "corpus").exists():
        # Dataset not downloaded (e.g. a fork PR without the cache): fall back to the synth smoke
        # set, report only
        dataset = "synth"
        report_only = True
        print(f"\n[retrieval-gate] DomainRAG 数据缺失 → 回退 {dataset}（report-only）")

    report = run_experiment(
        profile, dataset=dataset, track="retrieval", output_dir=Path(__file__).parent / "reports"
    )

    assert not report["errored"], f"检索 case 出错：{report['errored']}"
    assert report["latency_passed"], f"检索延迟超限：{report['latency_detail']}"
    if report_only:
        return  # the smoke fallback only checks the pipeline, it does not catch regressions
    assert report["retrieval"]["passed"], (
        f"检索轨门禁未达标（recall/MRR/hit 任一低于阈值）：{report['retrieval']['detail']}"
    )
