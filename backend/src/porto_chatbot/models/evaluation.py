from __future__ import annotations

from pydantic import BaseModel

# ----------------------------- Evaluation 结果模型 ----------------------------- #


class WorkflowCheck(BaseModel):
    """workflow 评估中单项检查的结果。"""

    name: str
    passed: bool
    weight: int


class WorkflowEvaluation(BaseModel):
    """evaluate_workflow() 的结构化返回值。

    spec_rubric_avg / spec_rubric_min 由 evaluate 节点在 spec refine loop
    完成后动态注入（基础评估阶段为 None）。
    """

    score: int
    passed: bool
    checks: list[WorkflowCheck]
    prd_chars: int
    spec_rubric_avg: float | None = None
    spec_rubric_min: float | None = None


class RagMetrics(BaseModel):
    """RAG 案例评估的 RAGAS 维度指标。"""

    answer_relevance: float
    context_relevance: float
    groundedness: float
    faithfulness: float


class RagCaseEvaluation(BaseModel):
    """单个 RAG 案例的评估结果。"""

    question: str
    score: float
    passed: bool
    metrics: RagMetrics
    notes: str


class RagBatchEvaluation(BaseModel):
    """一批 RAG 案例的聚合评估结果。"""

    score: float
    passed: bool
    cases: list[RagCaseEvaluation]
