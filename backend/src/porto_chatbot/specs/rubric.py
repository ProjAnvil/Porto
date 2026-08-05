"""规格评审 rubric 常量与辅助函数。"""
from __future__ import annotations

from ..models.enums import SpecVerdict

# ----------------------------- Rubric ----------------------------- #
# 6 维 × 0-2 分，满分 12。≥ spec_refine_pass_score(默认10) 为 PASS。

SPEC_RUBRIC: list[dict[str, str]] = [
    {"key": "coverage", "name": "需求覆盖度", "desc": "PRD/understanding 中与本子系统职责相关的需求是否都映射成了能力项"},
    {"key": "api", "name": "API 规范性", "desc": "端点、HTTP 方法、输入/输出、关键错误码是否清晰且可被调用方验证"},
    {"key": "data_model", "name": "数据模型完整性", "desc": "核心实体、关键字段、归属关系是否齐全（不止列名字）"},
    {"key": "dependency", "name": "依赖与边界", "desc": "与其他子系统的同步/异步依赖是否显式声明，数据所有权是否清晰"},
    {"key": "verifiability", "name": "可验证性", "desc": "验收标准是否具体可测（不是空话）"},
    {"key": "consistency", "name": "一致性", "desc": "与 understanding 报告、知识库片段是否冲突"},
]

_SPEC_SECTIONS = ["执行摘要", "业务能力", "API 需求", "数据模型需求", "集成需求", "验收标准"]


def _rubric_text() -> str:
    return "\n".join(f"- {r['name']}（{r['key']}）：{r['desc']}" for r in SPEC_RUBRIC)


def _critique_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": [e.value for e in SpecVerdict]},
            "score": {"type": "integer", "minimum": 0, "maximum": 12},
            "feedback": {"type": "string", "description": "针对未满分维度的具体改进建议"},
            "per_dimension": {
                "type": "object",
                "description": "每维分数 0-2",
                "properties": {r["key"]: {"type": "integer"} for r in SPEC_RUBRIC},
            },
        },
        "required": ["verdict", "score", "feedback"],
    }
