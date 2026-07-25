"""Evaluator-Optimizer Loop：四重终止的规格迭代主循环。"""

from __future__ import annotations

from ..models import SpecAttempt, SpecResult, Subsystem
from .context import SpecContext
from .steps import critique_spec, generate_initial_spec, refine_spec
from .template import render_template_spec

# ----------------------------- Evaluator-Optimizer Loop ----------------------------- #


def generate_spec_with_loop(
    ctx: SpecContext, sub: Subsystem, *, max_iter: int | None = None
) -> SpecResult:
    """对单个子系统运行 generate→critique→ refine loop，四重终止。"""
    settings = ctx.settings
    if not (ctx.llm.enabled and settings.spec_refine_enabled):
        return SpecResult(final=render_template_spec(ctx, sub), used_llm=False, tool_meta={})

    resolved_max = max_iter if max_iter is not None else settings.spec_refine_max_iter
    # 粗略 token 预算：按 4 字符 ≈ 1 token 估算（条件④的简易实现）
    budget_chars = settings.spec_refine_budget_tokens * 4

    spec, tool_meta = generate_initial_spec(ctx, sub)
    # tool-calling 截断:spec 已是固定提示,跳过 critique/refine
    if tool_meta.get("truncated"):
        return SpecResult(
            final=spec,
            attempts=[],
            iterations=0,
            truncated=False,
            used_llm=True,
            tool_meta=tool_meta,
        )
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
            attempts.append(
                SpecAttempt(version=i, verdict="NEEDS_IMPROVEMENT", feedback_digest="critic 不可用")
            )
            break

        attempts.append(
            SpecAttempt(
                version=i,
                score=critique.score,
                verdict=critique.verdict,
                feedback_digest=critique.feedback[:200],
            )
        )
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
        tool_meta=tool_meta,
    )
