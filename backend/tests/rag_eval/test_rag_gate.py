"""RAG 质量门禁——``run_experiment()`` 的 pytest 薄封装。

默认跑 ``rerank`` profile（P0 改进配置）；可通过 env 切换::

    RAG_EVAL_PROFILE=baseline pytest ...        # 换 profile
    RAG_EVAL_MAX_CASES=5 pytest ...             # 减 case 数
    RAG_EVAL_REPORT_ONLY=0 pytest ...           # 启用硬门禁（低分 fail）

CLI 实验请用 ``python -m tests.rag_eval.experiment``（支持 --sweep / --compare）。
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

    report = run_experiment(profile, env=gate_env)

    # 门禁报告写 .last_report.json（向后兼容），run_experiment 同时写 reports/<name>_<ts>.json
    _LAST_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # errored 始终 fail（真故障）；低分默认 report-only
    assert not report["errored"], f"有 case 出错（真故障）：{report['errored']}"
    if os.environ.get("RAG_EVAL_REPORT_ONLY", "1") != "1":
        assert report["passed"], f"RAG 门禁未达标：{report['detail']}"
