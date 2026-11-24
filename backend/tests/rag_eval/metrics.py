from __future__ import annotations

import os
import time
from typing import Any

# Batch-mean gate thresholds (start report-only, then tune from baselines). Lenient on
# purpose to avoid flakes.
THRESHOLDS: dict[str, float] = {
    "faithfulness": 0.55,
    "answer_relevancy": 0.50,
    "answer_correctness": 0.45,
    "contextual_precision": 0.50,
    "contextual_recall": 0.50,
    "contextual_relevancy": 0.50,
}

# Latency gate (seconds, p95). Eval overhead is infrastructure and is not counted; only the
# product path (retrieval + generation) is gated. Override with
# RAG_EVAL_RETRIEVE_P95_MAX / RAG_EVAL_GENERATE_P95_MAX.
LATENCY_THRESHOLDS: dict[str, float] = {
    "retrieve_s": float(os.environ.get("RAG_EVAL_RETRIEVE_P95_MAX", "10.0")),
    "generate_s": float(os.environ.get("RAG_EVAL_GENERATE_P95_MAX", "90.0")),
}

# Retries for a failed judge call on one case (retries+1 attempts in total; works around the
# occasional "Evaluation LLM outputted an invalid JSON")
METRIC_RETRIES = 2

# Above this share of errored cases the experiment is invalid (judge / environment failure,
# the means cannot be trusted)
MAX_ERROR_RATE = 0.10

_CORRECTNESS_CRITERIA = (
    "判断「实际答案」是否在事实上与「参考答案」一致——语义等价即可，"
    "允许措辞、语序与详略不同；存在事实矛盾、关键信息缺失或臆造则扣分。"
)


def build_metrics(model: str | Any) -> dict[str, Any]:
    """Instantiate the 6 DeepEval metrics (threshold=0: no per-case verdict, aggregate judges the
    batch as a whole).

    deepeval 4.x has no AnswerCorrectnessMetric, so answer correctness is a GEval (CoT LLM
    judge) scoring actual_output against expected_output.

    ``model`` may be a string or a deepeval ``DeepEvalBaseLLM`` instance. For a self-hosted
    OpenAI-compatible endpoint (DeepSeek / vLLM, ...) you **must pass an instance**: a string
    falls back to ``GPTModel`` without ``base_url`` and gets a 401 (see
    conftest._build_judge_model).
    """
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        ContextualRecallMetric,
        ContextualRelevancyMetric,
        FaithfulnessMetric,
        GEval,
    )
    from deepeval.test_case import SingleTurnParams

    return {
        "faithfulness": FaithfulnessMetric(threshold=0.0, model=model),
        "answer_relevancy": AnswerRelevancyMetric(threshold=0.0, model=model),
        "answer_correctness": GEval(
            name="answer_correctness",
            criteria=_CORRECTNESS_CRITERIA,
            evaluation_params=[SingleTurnParams.ACTUAL_OUTPUT, SingleTurnParams.EXPECTED_OUTPUT],
            model=model,
            threshold=0.0,
        ),
        "contextual_precision": ContextualPrecisionMetric(threshold=0.0, model=model),
        "contextual_recall": ContextualRecallMetric(threshold=0.0, model=model),
        "contextual_relevancy": ContextualRelevancyMetric(threshold=0.0, model=model),
    }


def evaluate_case(tc: Any, metrics: dict[str, Any], *, retries: int = METRIC_RETRIES) -> dict[str, float]:
    """Run every metric on one LLMTestCase and return {metric_name: score}.

    A failing metric (occasional errors such as judge JSON parsing) is retried with backoff
    ``retries`` times and only raised if it keeps failing (run_experiment then records it in
    errored).
    """
    scores: dict[str, float] = {}
    for name, metric in metrics.items():
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                metric.measure(tc)
                scores[name] = float(metric.score or 0.0)
                last_exc = None
                break
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        if last_exc is not None:
            raise last_exc
    return scores


def aggregate(per_case: list[dict[str, float]]) -> dict[str, float]:
    """Mean of each metric across cases (a pure function, no deepeval dependency)."""
    if not per_case:
        return {k: 0.0 for k in THRESHOLDS}
    keys = THRESHOLDS.keys()
    return {k: round(sum(c.get(k, 0.0) for c in per_case) / len(per_case), 4) for k in keys}


def judge(per_metric_mean: dict[str, float]) -> tuple[bool, dict[str, dict]]:
    """Compare against THRESHOLDS: pass only if every metric clears its threshold. Returns
    (passed, {metric: {mean, threshold, passed}})."""
    detail: dict[str, dict] = {}
    for k, thr in THRESHOLDS.items():
        mean = per_metric_mean.get(k, 0.0)
        detail[k] = {"mean": mean, "threshold": thr, "passed": mean >= thr}
    passed = all(d["passed"] for d in detail.values())
    return passed, detail


# ────────────────────────────────────────────────────────────────────────── #
# Latency gate: timings are collected in run_rag / run_experiment; add p50/p95 and the
# assertion here
# ────────────────────────────────────────────────────────────────────────── #


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation quantile (friendly to small samples; an empty list returns 0)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return round(s[0], 2)
    rank = (len(s) - 1) * pct / 100.0
    lo, hi = int(rank), min(int(rank) + 1, len(s) - 1)
    frac = rank - lo
    return round(s[lo] + (s[hi] - s[lo]) * frac, 2)


def latency_summary(timings: list[dict]) -> dict[str, dict]:
    """p50/p95 over the per-case timing list (keys are fixed: retrieve_s / generate_s)."""
    out: dict[str, dict] = {}
    for key in ("retrieve_s", "generate_s"):
        vals = [t[key] for t in timings if key in t]
        if vals:
            out[key] = {"p50": percentile(vals, 50), "p95": percentile(vals, 95)}
    return out


def judge_latency(latency: dict[str, dict]) -> tuple[bool, dict[str, dict]]:
    """Compare against LATENCY_THRESHOLDS (p95 caps). Returns (passed, detail)."""
    detail: dict[str, dict] = {}
    for k, thr in LATENCY_THRESHOLDS.items():
        p95 = latency.get(k, {}).get("p95", 0.0)
        detail[k] = {"p95": p95, "threshold": thr, "passed": p95 <= thr}
    passed = all(d["passed"] for d in detail.values())
    return passed, detail
