from __future__ import annotations

import asyncio

from ...tools import AgentToolContext
from ..heuristics import extract_bullets, extract_entities, matched_domains, summary_sentence
from ..state import PortoAgentState

_TRUNCATED_NOTICE_TOOL = (
    "⚠️ 业务理解未能完成：本步工具调用已达上限（{calls}/{limit} turn）。建议重跑本步。"
)
_TRUNCATED_NOTICE_TOKENS = (
    "⚠️ 业务理解未能完成：输出长度已达上限（max_tokens），自动升级 + 续写仍未收敛。建议重跑本步。"
)


def understand_prd(state, *, config):
    agent = config["configurable"]["agent"]
    agent.logger.info("step understand_prd start workflow_id=%s", state.get("workflow_id"))
    max_turns = agent.settings.agent_max_tool_turns
    understanding = ""
    tool_meta = {"turns": 0, "tool_calls": 0, "truncated": False,
                 "max_turns": max_turns, "reason": None}
    if agent.llm.enabled:
        ctx = AgentToolContext(state=state, vector_store=agent.vector_store)
        result = asyncio.run(
            agent.backend.execute_node(
                system=(
                    "你是资深业务分析师。根据 PRD 和知识库片段，输出简洁的中文业务理解报告，"
                    "包含：执行摘要、业务目标、核心实体、子系统线索。"
                    "可调用工具获取 PRD 原文与检索知识库，自主决定检索什么。"
                ),
                user="请生成业务理解报告。",
                tools=agent.backend.build_tools(ctx),
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
            if result.reason == "max_tokens_truncated":
                understanding = _TRUNCATED_NOTICE_TOKENS
            else:
                understanding = _TRUNCATED_NOTICE_TOOL.format(
                    calls=tool_meta["tool_calls"], limit=max_turns)
            agent.logger.info(
                "step understand_prd truncated workflow_id=%s reason=%s turns=%s calls=%s",
                state.get("workflow_id"), result.reason, result.turns,
                len(result.tool_calls))
        else:
            understanding = (result.text or "").strip()
            agent.logger.info(
                "step understand_prd llm tool_calls=%s turns=%s chars=%s",
                len(result.tool_calls), result.turns, len(understanding))
    if not understanding:
        understanding = _fallback_understanding(state)
        agent.logger.info(
            "step understand_prd used fallback workflow_id=%s", state.get("workflow_id"))
    return {
        "understanding": understanding,
        "current_step": "understand",
        **agent._step(
            "understand_prd",
            "完成业务理解报告",
            {"chars": len(understanding), "used_llm": bool(understanding) and agent.llm.enabled,
             "tool_meta": tool_meta},
        ),
    }


def _fallback_understanding(state: PortoAgentState) -> str:
    text = state["prd_text"]
    goals = extract_bullets(text, ["目标", "需要", "实现", "支持", "管理"])
    entities = extract_entities(text)
    return "\n".join(
        [
            "# Step 1: 业务需求理解",
            "",
            f"## 1. 执行摘要\n{summary_sentence(text)}",
            "",
            "## 2. 业务目标",
            *(f"- {g}" for g in goals[:6]),
            "",
            "## 3. 核心实体",
            *(f"- {e}" for e in entities[:12]),
            "",
            "## 4. 子系统线索",
            *(
                f"- {name}-service: {', '.join(hints[:3])}"
                for name, hints in matched_domains(text).items()
            ),
        ]
    )
