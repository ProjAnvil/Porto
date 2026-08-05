from __future__ import annotations

from porto_chatbot.evaluation import evaluate_workflow
from porto_chatbot.models import Subsystem


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
