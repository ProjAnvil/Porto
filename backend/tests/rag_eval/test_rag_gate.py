"""RAG quality gate (end-to-end track): a thin pytest wrapper around
``run_experiment(track="e2e")``.

Runs the ``rerank`` profile by default; switch through env::

    RAG_EVAL_PROFILE=baseline pytest ...        # switch profile
    RAG_EVAL_DATASET=domainrag:multidoc pytest  # switch dataset
    RAG_EVAL_MAX_CASES=5 pytest ...             # fewer cases
    RAG_EVAL_REPORT_ONLY=0 pytest ...           # enable the hard gate (low scores fail)

The retrieval-track hard gate (zero LLM, run on every PR) is in
``test_retrieval_gate.py``; the refusal gate is in ``test_refusal_gate.py``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from .experiment import run_experiment
from .profiles import get_profile

pytestmark = pytest.mark.integration

_LAST_REPORT = Path(__file__).resolve().parent / ".last_report.json"


def test_rag_quality_gate(gate_env):
    profile = get_profile(os.environ.get("RAG_EVAL_PROFILE", "rerank"))
    if max_cases := os.environ.get("RAG_EVAL_MAX_CASES"):
        profile.max_cases = int(max_cases)
    dataset = os.environ.get("RAG_EVAL_DATASET", "domainrag")

    report = run_experiment(profile, env=gate_env, dataset=dataset, track="e2e")

    # The gate report goes to .last_report.json (for backward compatibility); run_experiment
    # also writes reports/<name>_<ts>.json
    _LAST_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # errored cases / an invalid experiment always fail (a real fault); low scores are
    # report-only by default
    assert not report["errored"], f"有 case 出错（真故障）：{report['errored']}"
    assert not report["invalid"], (
        f"errored 率 {report['error_rate']:.0%} > 10%——实验无效（judge/环境故障），"
        "均值不可信，不计为回归证据"
    )
    if os.environ.get("RAG_EVAL_REPORT_ONLY", "1") != "1":
        assert report["passed"], (
            f"RAG 门禁未达标：{report['detail']} | latency={report['latency']} "
            f"| retrieval={report['retrieval']['detail']}"
        )
