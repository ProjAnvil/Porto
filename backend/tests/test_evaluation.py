from __future__ import annotations

import pytest

from porto_chatbot.evaluation import evaluate_rag_case, evaluate_workflow
from porto_chatbot.models import EvalCase, Subsystem


def test_evaluation_scores_complete_workflow(sample_prd):
    subsystems = [
        Subsystem(
            name="payment-service",
            responsibility="负责支付",
            capabilities=["支付发起"],
            data_entities=["Payment"],
        )
    ]
    specs = {"payment-service": "# payment-service\n\n## API 需求\n\n## 数据模型需求\n"}

    result = evaluate_workflow(sample_prd, "x" * 140, subsystems, specs)

    assert result.score == 100
    assert result.passed is True


# ── correctness 维度（ground_truth 存在时启用第五维）───────────────────── #


def _case(answer: str, ground_truth: str | None) -> EvalCase:
    return EvalCase(
        question="支付服务负责什么？",
        answer=answer,
        contexts=["payment-service 负责支付授权、退款、结算与渠道路由。"],
        ground_truth=ground_truth,
    )


def test_rag_case_without_ground_truth_keeps_four_dims():
    r = evaluate_rag_case(_case("支付服务负责支付授权与退款。", None))
    assert r.metrics.answer_correctness is None
    # 四维权重不变：手算一次确保公式稳定（metrics 已 4 位舍入 → 近似比较）
    m = r.metrics
    expected = 100 * (
        0.30 * max(0.0, m.answer_relevance)
        + 0.25 * max(0.0, m.context_relevance)
        + 0.25 * m.groundedness
        + 0.20 * m.faithfulness
    )
    assert r.score == pytest.approx(expected, abs=0.05)


def test_rag_case_with_ground_truth_scores_correctness():
    r_same = evaluate_rag_case(_case("支付授权、退款、结算与渠道路由。",
                                     "支付授权、退款、结算与渠道路由。"))
    assert r_same.metrics.answer_correctness == 1.0  # 完全一致 → F1=1

    r_diff = evaluate_rag_case(_case("量子隧穿效应的概率幅。", "支付授权、退款。"))
    assert r_diff.metrics.answer_correctness == 0.0  # 完全不相干 → F1=0

    # 同答案不同 ground_truth → correctness 有差异，且影响总分
    r_partial = evaluate_rag_case(_case("支付授权与退款。", "支付授权、退款、结算。"))
    assert 0.0 < r_partial.metrics.answer_correctness < 1.0
    assert r_partial.score != r_same.score or r_same.metrics.answer_correctness == 1.0


def test_rag_case_correctness_in_weighted_score():
    r = evaluate_rag_case(_case("支付授权、退款、结算与渠道路由。",
                                "支付授权、退款、结算与渠道路由。"))
    m = r.metrics
    expected = 100 * (
        0.25 * max(0.0, m.answer_relevance)
        + 0.20 * max(0.0, m.context_relevance)
        + 0.20 * m.groundedness
        + 0.15 * m.faithfulness
        + 0.20 * m.answer_correctness
    )
    assert r.score == pytest.approx(expected, abs=0.05)
