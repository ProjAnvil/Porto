"""检索轨硬门禁——确定性检索指标（recall@k / MRR / hit@k）+ 检索延迟 p95。

双轨评估的 PR 门禁：零 judge LLM；baseline profile（local embedding、NONE
策略、无 rerank）下**完全零 LLM**，无密钥也必须能跑——检索回归在此拦截。

数据集：默认 DomainRAG extractive（缺数据时自动回退 synth 冒烟集并降级为
report-only，避免 CI 无缓存时假绿/假红）。env::

    RAG_EVAL_RETRIEVAL_PROFILE=smallchunk pytest ...  # 默认 baseline（零 LLM）
    RAG_EVAL_RETRIEVAL_DATASET=domainrag:multidoc pytest
    RAG_EVAL_RECALL_MIN=0.5 pytest ...                # 覆盖阈值（metrics/retrieval_metrics 读取）
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
        # 数据集未下载（如无 cache 的 fork PR）：回退 synth 冒烟，仅报告
        dataset = "synth"
        report_only = True
        print(f"\n[retrieval-gate] DomainRAG 数据缺失 → 回退 {dataset}（report-only）")

    report = run_experiment(
        profile, dataset=dataset, track="retrieval", output_dir=Path(__file__).parent / "reports"
    )

    assert not report["errored"], f"检索 case 出错：{report['errored']}"
    assert report["latency_passed"], f"检索延迟超限：{report['latency_detail']}"
    if report_only:
        return  # 冒烟回退只验证链路，不拦回归
    assert report["retrieval"]["passed"], (
        f"检索轨门禁未达标（recall/MRR/hit 任一低于阈值）：{report['retrieval']['detail']}"
    )
