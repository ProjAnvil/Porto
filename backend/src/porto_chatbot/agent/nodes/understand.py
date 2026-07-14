from __future__ import annotations

from ...tools import AgentToolContext, build_agent_tools
from ..heuristics import extract_bullets, extract_entities, matched_domains, summary_sentence
from ..state import PortoAgentState


def understand_prd(agent, state: PortoAgentState) -> PortoAgentState:
    agent.logger.info("step understand_prd start workflow_id=%s", state["workflow_id"])
    understanding = ""
    if agent.llm.enabled:
        ctx = AgentToolContext(state=state, vector_store=agent.vector_store)
        result = agent.llm.complete_with_tools(
            "你是资深业务分析师。根据 PRD 和知识库片段，输出简洁的中文业务理解报告，"
            "包含：执行摘要、业务目标、核心实体、子系统线索。"
            "可调用工具获取 PRD 原文与检索知识库，自主决定检索什么。",
            "请生成业务理解报告。",
            build_agent_tools(ctx),
        )
        understanding = (result.text or "").strip()
        agent.logger.info(
            "step understand_prd llm tool_calls=%s turns=%s chars=%s",
            len(result.tool_calls),
            result.turns,
            len(understanding),
        )
    if not understanding:
        understanding = _fallback_understanding(state)
        agent.logger.info("step understand_prd used fallback workflow_id=%s", state["workflow_id"])
    return agent._with_step(
        {**state, "understanding": understanding},
        "understand_prd",
        "完成业务理解报告",
        {"chars": len(understanding), "used_llm": bool(understanding) and agent.llm.enabled},
    )


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
            *(f"- {name}-service: {', '.join(hints[:3])}" for name, hints in matched_domains(text).items()),
        ]
    )
