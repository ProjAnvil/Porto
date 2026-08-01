"""Skill definitions and deployment.

Code is the single source of truth — SKILL.md files are generated products.
Langchain mode uses the code strings directly; Agent SDK mode uses the files
written by :func:`deploy_skills`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..specs.rubric import _SPEC_SECTIONS, _rubric_text


@dataclass(frozen=True)
class SkillDefinition:
    """A single Agent SDK skill (``SKILL.md`` body + frontmatter description)."""

    description: str
    body: str


# ----------------------------- Prompt bodies ----------------------------- #
# Sourced from the inline system prompts in the langchain node/step functions.
# These start identical to the langchain prompts but may diverge over time —
# the skill body is the *static* part shown to the Agent SDK; per-subsystem /
# per-session context is supplied at runtime by the orchestrator.

_UNDERSTAND_PROMPT = (
    "你是资深业务分析师。根据 PRD 和知识库片段，输出简洁的中文业务理解报告，"
    "包含：执行摘要、业务目标、核心实体、子系统线索。"
    "可调用工具获取 PRD 原文与检索知识库，自主决定检索什么。"
)

_IDENTIFY_PROMPT = (
    "你是资深系统架构师。按领域驱动设计原则，根据业务理解报告与 PRD 识别需要拆分的子系统。"
    "每个子系统职责单一、边界清晰，数量控制在 2-6 个，命名形如 xxx-service。"
)

_GENERATE_PROMPT = (
    f"你是资深系统规格工程师。生成详细的系统需求规格（markdown）。"
    f"必须包含这些章节：{', '.join(_SPEC_SECTIONS)}。"
    "API 需求要给出具体端点/方法/输入输出/错误码；数据模型要列实体与关键字段；验收标准要具体可测。"
    "可调用工具检索知识库以参考现有系统约定。"
)

_EVALUATE_PROMPT = (
    "你是严格的系统规格评审专家。只评审、不重写。依据如下 rubric 打分：\n"
    f"{_rubric_text()}"
)

_MEMORY_GUIDE = (
    "# Porto Memory 系统使用指南\n\n"
    "Porto 有三层 memory 系统：\n"
    "1. **search_memory**: 跨会话语义检索对话记忆。当用户提到之前讨论过的内容时调用。\n"
    "2. **get_session_facts**: 读取当前会话的结构化关键事实（4 类：决策/偏好/背景/待澄清）。\n"
    "   优先参考 facts——它们是用户已确认的信息。\n"
    "3. **search_knowledgebase**: 在 SCV 生成的知识库中检索文档片段。\n\n"
    "调用时机：不确定时先查 facts 和 memory，再查 knowledgebase。寒暄闲聊不需要调用任何工具。"
)


CLAUDE_MD = """# Porto — Codebase-Aware Spec Engineering

你是 Porto 的 spec 工程助手。你有以下工具和技能可用。

## 工具
- search_knowledgebase: 检索知识库文档片段
- search_memory: 跨会话语义检索对话记忆
- get_session_facts: 读取结构化关键事实
- get_prd_text / get_understanding / list_subsystems / get_subsystem / get_sources: 工作流状态访问

## 行为准则
- 优先基于工具返回的信息回答，不确定时说明缺口
- 寒暄闲聊直接回答，不需要调用工具
- 结构化事实（facts）优先级最高——它们是用户已确认的信息
"""


SKILLS: dict[str, SkillDefinition] = {
    "prd-analysis": SkillDefinition(
        description="理解 PRD 业务意图，提取核心需求和技术约束",
        body=_UNDERSTAND_PROMPT,
    ),
    "subsystem-decomposition": SkillDefinition(
        description="识别子系统及其职责、能力、数据实体、依赖关系",
        body=_IDENTIFY_PROMPT,
    ),
    "spec-generation": SkillDefinition(
        description="生成子系统规格并迭代优化（generate-critique-refine loop）",
        body=_GENERATE_PROMPT,
    ),
    "spec-evaluation": SkillDefinition(
        description="按 rubric 评估 spec 质量，决定是否需要返工",
        body=_EVALUATE_PROMPT,
    ),
    "porto-memory": SkillDefinition(
        description="Porto memory 系统使用指南：何时查 facts、何时查记忆",
        body=_MEMORY_GUIDE,
    ),
}


def deploy_skills(data_dir: Path) -> None:
    """Deploy CLAUDE.md and SKILL.md files from code templates.

    Called at backend startup. Idempotent — overwrites each time.
    Code changes to prompts sync to skill files on next restart.
    """
    claude_dir = data_dir / ".claude"
    skills_dir = claude_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    (claude_dir / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")

    for name, skill in SKILLS.items():
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        content = (
            f"---\n"
            f"name: {name}\n"
            f"description: {skill.description}\n"
            f"---\n\n"
            f"{skill.body}\n"
        )
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
