from __future__ import annotations

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


def evaluate_case(tc: Any, metrics: dict[str, Any]) -> dict[str, float]:
    """对单个 LLMTestCase 跑全部指标，返回 {metric_name: score}。"""
    scores: dict[str, float] = {}
    for name, metric in metrics.items():
        metric.measure(tc)
        scores[name] = float(metric.score or 0.0)
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
