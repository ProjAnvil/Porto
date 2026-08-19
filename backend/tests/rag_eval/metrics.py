from __future__ import annotations

import os
import time
from typing import Any

# 批量均值门禁阈值（report-only 起步后据基线调整）。偏松防 flaky。
THRESHOLDS: dict[str, float] = {
    "faithfulness": 0.55,
    "answer_relevancy": 0.50,
    "answer_correctness": 0.45,
    "contextual_precision": 0.50,
    "contextual_recall": 0.50,
    "contextual_relevancy": 0.50,
}

# 延迟门禁（秒，p95；eval 是基础设施开销不计入，只门产品链路的检索+生成）。
# 可用 RAG_EVAL_RETRIEVE_P95_MAX / RAG_EVAL_GENERATE_P95_MAX 覆盖。
LATENCY_THRESHOLDS: dict[str, float] = {
    "retrieve_s": float(os.environ.get("RAG_EVAL_RETRIEVE_P95_MAX", "10.0")),
    "generate_s": float(os.environ.get("RAG_EVAL_GENERATE_P95_MAX", "90.0")),
}

# 单条 case 的 judge 失败重试次数（含首次共 retries+1 次；修复 judge 偶发
# "Evaluation LLM outputted an invalid JSON"）
METRIC_RETRIES = 2

# errored 占比超过该值判定实验无效（judge/环境故障，均值不可信）
MAX_ERROR_RATE = 0.10

_CORRECTNESS_CRITERIA = (
    "判断「实际答案」是否在事实上与「参考答案」一致——语义等价即可，"
    "允许措辞、语序与详略不同；存在事实矛盾、关键信息缺失或臆造则扣分。"
)


def build_metrics(model: str | Any) -> dict[str, Any]:
    """实例化 6 个 DeepEval 指标（threshold=0：逐条不判过，统一由 aggregate 批量判）。

    deepeval 4.x 没有 AnswerCorrectnessMetric，答案正确性用 GEval（CoT LLM-judge）
    对 actual_output vs expected_output 评判实现。

    ``model`` 可为字符串或 deepeval ``DeepEvalBaseLLM`` 实例。对自建 OpenAI-compatible
    端点（DeepSeek/vLLM 等）**必须传实例**——字符串会 fallback 到 ``GPTModel`` 但不传
    ``base_url``，导致 401（见 conftest._build_judge_model）。
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
    """对单个 LLMTestCase 跑全部指标，返回 {metric_name: score}。

    单指标失败（judge JSON 解析等偶发错误）按 ``retries`` 退避重试，仍失败才抛出
    （由 run_experiment 记入 errored）。
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
    """各指标跨 case 取均值（纯函数，不依赖 deepeval）。"""
    if not per_case:
        return {k: 0.0 for k in THRESHOLDS}
    keys = THRESHOLDS.keys()
    return {k: round(sum(c.get(k, 0.0) for c in per_case) / len(per_case), 4) for k in keys}


def judge(per_metric_mean: dict[str, float]) -> tuple[bool, dict[str, dict]]:
    """对比 THRESHOLDS：全指标达标才 pass。返回 (passed, {metric: {mean, threshold, passed}})。"""
    detail: dict[str, dict] = {}
    for k, thr in THRESHOLDS.items():
        mean = per_metric_mean.get(k, 0.0)
        detail[k] = {"mean": mean, "threshold": thr, "passed": mean >= thr}
    passed = all(d["passed"] for d in detail.values())
    return passed, detail


# ────────────────────────────────────────────────────────────────────────── #
# 延迟门禁：timing 已在 run_rag/run_experiment 采集，这里补 p50/p95 与断言
# ────────────────────────────────────────────────────────────────────────── #


def percentile(values: list[float], pct: float) -> float:
    """线性插值分位数（小样本友好；空列表返回 0）。"""
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
    """对 case 级 timing 列表算 p50/p95（键固定 retrieve_s / generate_s）。"""
    out: dict[str, dict] = {}
    for key in ("retrieve_s", "generate_s"):
        vals = [t[key] for t in timings if key in t]
        if vals:
            out[key] = {"p50": percentile(vals, 50), "p95": percentile(vals, 95)}
    return out


def judge_latency(latency: dict[str, dict]) -> tuple[bool, dict[str, dict]]:
    """对照 LATENCY_THRESHOLDS（p95 上限）。返回 (passed, detail)。"""
    detail: dict[str, dict] = {}
    for k, thr in LATENCY_THRESHOLDS.items():
        p95 = latency.get(k, {}).get("p95", 0.0)
        detail[k] = {"p95": p95, "threshold": thr, "passed": p95 <= thr}
    passed = all(d["passed"] for d in detail.values())
    return passed, detail
