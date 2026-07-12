"""子系统规格的生成与 evaluator-optimizer loop。

固定 workflow 的 generate_specs 节点内部，对每个子系统运行：
    generate → critique → refine → critique → …
直到 PASS / max_iter / 分数不升 / 预算上限（四重终止）。

依据：Anthropic Evaluator-Optimizer workflow + Self-Refine。
官方示例缺 max-iter guard，本实现显式补齐四重终止。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .llm import LLMClient
from .models import Critique, SpecAttempt, SpecResult, Subsystem
from .settings import Settings
from .tools import AgentToolContext, build_agent_tools
from .vector_store import LocalVectorStore

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
            "verdict": {"type": "string", "enum": ["PASS", "NEEDS_IMPROVEMENT", "FAIL"]},
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


@dataclass
class SpecContext:
    llm: LLMClient
    state: dict[str, Any]
    settings: Settings
    vector_store: LocalVectorStore | None = None


# ----------------------------- 模板生成（fallback）----------------------------- #

def render_template_spec(ctx: SpecContext, sub: Subsystem) -> str:
    """模板拼接的规格（搬自原 agent._render_spec），作为 LLM 不可用时的降级。"""
    state = ctx.state
    source_refs = ", ".join(s.path for s in state.get("sources", [])[:3]) or "无"
    capabilities = "\n".join(
        f"| BC-{i + 1:03d} | {cap} | P0 | 来自 PRD 和知识库匹配 |"
        for i, cap in enumerate(sub.capabilities)
    ) or "| BC-001 | （待补充） | P0 | 模板生成 |"
    entities = "\n".join(f"| {e} | 子系统拥有或引用的领域对象 |" for e in sub.data_entities) or "| （待补充） | - |"
    return f"""# {sub.name} - 系统需求

> 工作流 ID: {state.get('workflow_id', '')}
> 生成时间: {datetime.now(UTC).isoformat()}
> 知识库引用: {source_refs}

## 1. 执行摘要

| 属性 | 值 |
|------|-----|
| 名称 | {sub.name} |
| 类型 | {sub.type} |
| 职责 | {sub.responsibility} |

## 2. 业务能力

| ID | 能力 | 优先级 | 来源 |
|----|------|--------|------|
{capabilities}

## 3. API 需求

| 方法 | 端点 | 描述 |
|------|------|------|
| GET | /api/v1/{sub.name.replace("-service", "")} | 查询资源列表 |
| POST | /api/v1/{sub.name.replace("-service", "")} | 创建或触发核心业务动作 |
| GET | /api/v1/{sub.name.replace("-service", "")}/{{id}} | 查询资源详情 |

## 4. 数据模型需求

| 实体 | 描述 |
|------|------|
{entities}

## 5. 集成需求

- 同步接口通过 API Gateway 暴露。
- 异步集成建议通过领域事件解耦。
- 关键状态变更需要记录审计日志。

## 6. 验收标准

- [ ] 覆盖核心业务能力。
- [ ] API 输入输出和错误码可被调用方验证。
- [ ] 数据所有权边界清晰。
- [ ] 与相关子系统的同步/异步依赖可追踪。
"""


# ----------------------------- LLM 驱动的三步 ----------------------------- #

def generate_initial_spec(ctx: SpecContext, sub: Subsystem) -> str:
    """LLM 生成首版 spec（带工具，可检索知识库）。失败返回空串，由 loop 层降级。"""
    if not ctx.llm.enabled:
        return ""
    tools_ctx = AgentToolContext(state=ctx.state, vector_store=ctx.vector_store)
    result = ctx.llm.complete_with_tools(
        f"你是资深系统规格工程师。为子系统 {sub.name} 生成详细的系统需求规格（markdown）。"
        f"子系统职责：{sub.responsibility}；能力：{', '.join(sub.capabilities) or '（待识别）'}。"
        f"必须包含这些章节：{', '.join(_SPEC_SECTIONS)}。"
        "API 需求要给出具体端点/方法/输入输出/错误码；数据模型要列实体与关键字段；验收标准要具体可测。"
        "可调用工具检索知识库以参考现有系统约定。",
        f"请生成 {sub.name} 的规格文档。",
        build_agent_tools(tools_ctx),
    )
    return (result.text or "").strip()


def critique_spec(ctx: SpecContext, sub: Subsystem, spec: str) -> Critique | None:
    """LLM 依据 rubric 评判 spec。解析失败返回 None（loop 层接受当前版本）。"""
    if not ctx.llm.enabled:
        return None
    parsed = ctx.llm.complete_structured(
        "你是严格的系统规格评审专家。只评审、不重写。依据如下 6 维 rubric 打分，每维 0-2 分，满分 12：\n"
        f"{_rubric_text()}\n\n"
        "判定规则：score≥10 且无重大缺陷 → PASS；7-9 → NEEDS_IMPROVEMENT；≤6 → FAIL。"
        "feedback 必须针对未满分维度给出具体、可执行的改进方向。",
        f"子系统：{sub.name}（职责：{sub.responsibility}）\n\n待评审规格：\n{spec}",
        _critique_schema(),
    )
    if not isinstance(parsed, dict):
        return None
    verdict = parsed.get("verdict", "NEEDS_IMPROVEMENT")
    if verdict not in ("PASS", "NEEDS_IMPROVEMENT", "FAIL"):
        verdict = "NEEDS_IMPROVEMENT"
    try:
        score = int(parsed.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(12, score))
    per_dim = parsed.get("per_dimension")
    per_dim = per_dim if isinstance(per_dim, dict) else {}
    return Critique(verdict=verdict, score=score, feedback=str(parsed.get("feedback", "")), per_dimension=per_dim)


def refine_spec(ctx: SpecContext, sub: Subsystem, spec: str, feedback: str) -> str:
    """LLM 依据反馈修订 spec。失败返回原 spec（不改）。"""
    if not ctx.llm.enabled:
        return spec
    result = ctx.llm.complete(
        f"你是资深系统规格工程师。根据评审反馈改进 {sub.name} 的规格文档（职责：{sub.responsibility}）。"
        "保持原有 markdown 结构与章节，只针对反馈改进；不要删除已有合理内容；不要输出解释，直接给完整文档。",
        f"评审反馈：\n{feedback}\n\n当前规格：\n{spec}\n\n请输出改进后的完整规格文档。",
    )
    refined = (result or "").strip()
    return refined or spec


# ----------------------------- Evaluator-Optimizer Loop ----------------------------- #

def generate_spec_with_loop(ctx: SpecContext, sub: Subsystem, *, max_iter: int | None = None) -> SpecResult:
    """对单个子系统运行 generate→critique→ refine loop，四重终止。"""
    settings = ctx.settings
    if not (ctx.llm.enabled and settings.spec_refine_enabled):
        return SpecResult(final=render_template_spec(ctx, sub), used_llm=False)

    resolved_max = max_iter if max_iter is not None else settings.spec_refine_max_iter
    # 粗略 token 预算：按 4 字符 ≈ 1 token 估算（条件④的简易实现）
    budget_chars = settings.spec_refine_budget_tokens * 4

    spec = generate_initial_spec(ctx, sub)
    if not spec:
        spec = render_template_spec(ctx, sub)  # 生成失败降级模板
    used_chars = len(spec)

    attempts: list[SpecAttempt] = []
    best = spec
    best_score = -1
    truncated = False

    for i in range(1, resolved_max + 1):
        critique = critique_spec(ctx, sub, spec)
        if critique is None:
            # critic 不可用 → 接受当前 spec
            attempts.append(SpecAttempt(version=i, verdict="NEEDS_IMPROVEMENT", feedback_digest="critic 不可用"))
            break

        attempts.append(SpecAttempt(
            version=i,
            score=critique.score,
            verdict=critique.verdict,
            feedback_digest=critique.feedback[:200],
        ))
        used_chars += len(critique.feedback)

        # ① 达标
        if critique.verdict == "PASS" or critique.score >= settings.spec_refine_pass_score:
            best, best_score = spec, critique.score
            break
        # ③ 分数不升（震荡/退化）→ 回退 best
        if best_score >= 0 and critique.score <= best_score:
            break
        # 本次优于历史，更新 best
        best, best_score = spec, critique.score
        # ④ 预算上限
        if used_chars > budget_chars:
            truncated = True
            break
        # ② 达到 max_iter
        if i >= resolved_max:
            truncated = True
            break

        refined = refine_spec(ctx, sub, spec, critique.feedback)
        if refined and refined.strip():
            spec = refined
            used_chars += len(spec)

    if best_score < 0:
        best = spec  # critic 从未成功，用最新 spec

    return SpecResult(
        final=best,
        attempts=attempts,
        iterations=len(attempts),
        truncated=truncated,
        used_llm=True,
    )
