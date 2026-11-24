"""Refusal gate (negative rejection): when the knowledge base has no answer the system
must refuse explicitly instead of making one up.

The verdict is a lexical heuristic (``refusal_detector.py``, no judge LLM): the cost is
one generation call per question. Hard gate by default (accuracy ≥ 0.6); env::

    RAG_EVAL_REFUSAL_REPORT_ONLY=1 pytest ...   # report only, do not block
"""
from __future__ import annotations

import os

import pytest

from .experiment import run_experiment
from .profiles import get_profile

pytestmark = pytest.mark.integration


def test_refusal_gate(llm_env):
    profile = get_profile(os.environ.get("RAG_EVAL_REFUSAL_PROFILE", "baseline"))

    report = run_experiment(profile, env=llm_env, dataset="refusal", track="e2e")

    assert report["refusal"] is not None and report["refusal"]["n"] > 0
    assert not report["errored"], f"拒答 case 出错：{report['errored']}"
    wrong = [
        (r["question"], r["answer"])
        for r in report["cases"]
        if r.get("refusal") == 0.0
    ]
    for q, a in wrong:
        print(f"\n[refusal-gate] 未拒答: {q}\n  → {a}")
    if os.environ.get("RAG_EVAL_REFUSAL_REPORT_ONLY", "0") == "1":
        return
    assert report["refusal"]["passed"], (
        f"拒答正确率 {report['refusal']['accuracy']:.2f} < "
        f"{report['refusal']['threshold']}——系统在无依据时编造答案"
    )
