from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ----------------------------- Spec refine loop（Phase 2）----------------------------- #

Verdict = Literal["PASS", "NEEDS_IMPROVEMENT", "FAIL"]


class Critique(BaseModel):
    """critic 对单个 spec 版本的评判结果。"""

    verdict: Verdict
    score: int = Field(ge=0, le=12)
    feedback: str = ""
    per_dimension: dict[str, int] = Field(default_factory=dict)


class SpecAttempt(BaseModel):
    """loop 中一次迭代的快照（供可观测，不含完整 spec 全文）。"""

    version: int
    score: int = 0
    verdict: Verdict = "NEEDS_IMPROVEMENT"
    feedback_digest: str = ""


class SpecResult(BaseModel):
    """单个子系统 spec 的 loop 产物。"""

    final: str
    attempts: list[SpecAttempt] = Field(default_factory=list)
    iterations: int = 0
    truncated: bool = False          # refine-loop 截断(max_iter/budget),语义不动
    used_llm: bool = False
    tool_meta: dict = Field(default_factory=dict)  # tool-calling 元数据;truncated 键 = tool 截断
