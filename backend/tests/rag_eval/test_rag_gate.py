from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from porto_chatbot.models.enums import QueryTransformStrategy

from .metrics import aggregate, build_metrics, evaluate_case, judge
from .runner import run_rag

pytestmark = pytest.mark.integration

_REPORT = Path(__file__).resolve().parent / ".last_report.json"
# 检索变换策略：默认 none（基线）；可经 env 切换以横向对比 hyde/multi_query 等。
_STRATEGY = QueryTransformStrategy(os.environ.get("RAG_EVAL_STRATEGY", "none"))


def test_rag_quality_gate(eval_kb, domainrag_data, eval_llm, judge_env):
    _corpus, goldens = domainrag_data
    # 成本控制：默认仅评前 N 条（90 条 × 6 judge + 90 生成开销大）；env 可放开。
    max_cases = int(os.environ.get("RAG_EVAL_MAX_CASES", "20"))
    goldens = goldens[:max_cases] if max_cases > 0 else goldens

    metrics = build_metrics(f"openai/{judge_env}")

    per_case: list[dict[str, float]] = []
    case_reports: list[dict] = []
    errored: list[str] = []
    for idx, g in enumerate(goldens):
        try:
            tc, result = run_rag(g, eval_kb, eval_llm, strategy=_STRATEGY)
            scores = evaluate_case(tc, metrics)
        except Exception as e:  # noqa: BLE001
            errored.append(f"case#{idx} {g.question[:30]}: {e}")
            continue
        per_case.append(scores)
        case_reports.append(
            {"question": g.question, "scores": scores, "degraded": result.degraded}
        )

    mean = aggregate(per_case)
    passed, detail = judge(mean)
    _REPORT.write_text(
        json.dumps(
            {
                "n": len(goldens),
                "strategy": _STRATEGY.value,
                "mean": mean,
                "detail": detail,
                "cases": case_reports,
                "errored": errored,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # report-only 起步：默认只产出报告、不因低分 fail；errored 始终 fail（真故障）。
    report_only = os.environ.get("RAG_EVAL_REPORT_ONLY", "1") == "1"
    assert not errored, f"有 case 出错（真故障）：{errored}"
    if not report_only:
        assert passed, f"RAG 门禁未达标：{detail}"
