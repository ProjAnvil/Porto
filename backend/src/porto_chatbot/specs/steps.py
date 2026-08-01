"""LLM 驱动的三步：generate_initial_spec / critique_spec / refine_spec。"""

from __future__ import annotations

import asyncio

from ..models import Critique, Subsystem
from ..tools import AgentToolContext
from .context import SpecContext
from .rubric import _SPEC_SECTIONS, _critique_schema, _rubric_text

# ----------------------------- LLM 驱动的三步 ----------------------------- #

_TRUNCATED_NOTICE_SPEC_TOOL = (
    "⚠️ 规格生成未能完成：本子系统工具调用已达上限（{calls}/{limit} turn）。建议重跑本步。"
)
_TRUNCATED_NOTICE_SPEC_TOKENS = (
    "⚠️ 规格生成未能完成：输出长度已达上限（max_tokens），自动升级 + 续写仍未收敛。建议重跑本步。"
)


def generate_initial_spec(ctx: SpecContext, sub: Subsystem) -> tuple[str, dict]:
    """LLM 生成首版 spec(带工具)。返回 (spec_text, tool_meta)。

    tool 截断时 spec_text = 固定提示,由 loop 层跳过 critique/refine。
    LLM 未启用时返回 ("", 空 tool_meta)。
    """
    max_turns = ctx.settings.agent_max_tool_turns
    if not ctx.llm.enabled:
        return "", {
            "turns": 0,
            "tool_calls": 0,
            "truncated": False,
            "max_turns": max_turns,
            "reason": None,
        }
    tools_ctx = AgentToolContext(state=ctx.state, vector_store=ctx.vector_store)
    result = asyncio.run(
        ctx.backend.execute_node(
            system=(
                f"你是资深系统规格工程师。为子系统 {sub.name} 生成详细的系统需求规格（markdown）。"
                f"子系统职责：{sub.responsibility}；能力：{', '.join(sub.capabilities) or '（待识别）'}。"
                f"必须包含这些章节：{', '.join(_SPEC_SECTIONS)}。"
                "API 需求要给出具体端点/方法/输入输出/错误码；数据模型要列实体与关键字段；验收标准要具体可测。"
                "可调用工具检索知识库以参考现有系统约定。"
            ),
            user=f"请生成 {sub.name} 的规格文档。",
            tools=ctx.backend.build_tools(tools_ctx),
            max_turns=max_turns,
        )
    )
    tool_meta = {
        "turns": result.turns,
        "tool_calls": len(result.tool_calls),
        "truncated": result.truncated,
        "max_turns": max_turns,
        "reason": result.reason,
    }
    if result.truncated:
        notice = (
            _TRUNCATED_NOTICE_SPEC_TOKENS
            if result.reason == "max_tokens_truncated"
            else _TRUNCATED_NOTICE_SPEC_TOOL.format(
                calls=tool_meta["tool_calls"], limit=max_turns
            )
        )
        return notice, tool_meta
    return (result.text or "").strip(), tool_meta


def critique_spec(ctx: SpecContext, sub: Subsystem, spec: str) -> Critique | None:
    """LLM 依据 rubric 评判 spec。

    走 ``ctx.backend.execute_node(structured_schema=...)``——与
    generate/refine 同一个 backend，不跨 provider 混搭。
    解析失败返回 None（loop 层接受当前版本）。
    """
    if ctx.backend is None:
        return None
    result = asyncio.run(
        ctx.backend.execute_node(
            system=(
                "你是严格的系统规格评审专家。只评审、不重写。依据如下 6 维 rubric 打分，每维 0-2 分，满分 12：\n"
                f"{_rubric_text()}\n\n"
                "判定规则：score≥10 且无重大缺陷 → PASS；7-9 → NEEDS_IMPROVEMENT；≤6 → FAIL。"
                "feedback 必须针对未满分维度给出具体、可执行的改进方向。"
            ),
            user=(
                f"子系统：{sub.name}（职责：{sub.responsibility}）\n\n待评审规格：\n{spec}"
            ),
            structured_schema=_critique_schema(),
        )
    )
    parsed = result.structured
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
    return Critique(
        verdict=verdict,
        score=score,
        feedback=str(parsed.get("feedback", "")),
        per_dimension=per_dim,
    )


def refine_spec(ctx: SpecContext, sub: Subsystem, spec: str, feedback: str) -> str:
    """LLM 依据反馈修订 spec。失败返回原 spec（不改）。"""
    if not ctx.llm.enabled:
        return spec
    result = asyncio.run(
        ctx.backend.execute_node(
            system=(
                f"你是资深系统规格工程师。根据评审反馈改进 {sub.name} 的规格文档（职责：{sub.responsibility}）。"
                "保持原有 markdown 结构与章节，只针对反馈改进；不要删除已有合理内容；不要输出解释，直接给完整文档。"
            ),
            user=(
                f"评审反馈：\n{feedback}\n\n当前规格：\n{spec}\n\n请输出改进后的完整规格文档。"
            ),
        )
    )
    refined = (result.text or "").strip()
    return refined or spec
