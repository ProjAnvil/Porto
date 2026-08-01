from __future__ import annotations

import asyncio

from ...models import Subsystem
from ..heuristics import (
    capabilities_for,
    entities_for,
    matched_domains,
    normalize_sub_dict,
    responsibility_for,
    subsystem_schema,
)
from ..state import PortoAgentState


def identify_subsystems(state, *, config):
    agent = config["configurable"]["agent"]
    agent.logger.info("step identify_subsystems start workflow_id=%s", state["workflow_id"])
    subsystems: list[Subsystem] = []
    if agent.llm.enabled:
        result = asyncio.run(
            agent.backend.execute_node(
                system=(
                    "你是资深系统架构师。按领域驱动设计原则，根据业务理解报告与 PRD 识别需要拆分的子系统。"
                    "每个子系统职责单一、边界清晰，数量控制在 2-6 个，命名形如 xxx-service。"
                ),
                user=(
                    f"业务理解报告:\n{state['understanding']}\n\n"
                    f"PRD 节选:\n{state['prd_text'][:2000]}"
                ),
                structured_schema=subsystem_schema(),
            )
        )
        parsed = result.structured
        raw_list = (parsed or {}).get("subsystems", []) if isinstance(parsed, dict) else []
        for raw in raw_list:
            norm = normalize_sub_dict(raw)
            if norm:
                subsystems.append(Subsystem(**norm))
        agent.logger.info(
            "step identify_subsystems llm parsed=%s accepted=%s",
            len(raw_list),
            len(subsystems),
        )
    if not subsystems:
        subsystems = _fallback_identify(state)
        agent.logger.info(
            "step identify_subsystems used fallback workflow_id=%s", state["workflow_id"]
        )
    return {
        "subsystems": subsystems,
        "current_step": "identify",
        **agent._step(
            "identify_subsystems",
            f"识别 {len(subsystems)} 个子系统",
            {
                "subsystems": [s.model_dump() for s in subsystems],
                "used_llm": bool(subsystems) and agent.llm.enabled,
            },
        ),
    }


def _fallback_identify(state: PortoAgentState) -> list[Subsystem]:
    domains = matched_domains(state["prd_text"] + "\n" + state["understanding"])
    subsystems: list[Subsystem] = []
    for domain, matches in domains.items():
        name = f"{domain}-service"
        sources_text = " ".join(s.text for s in state.get("sources", []))
        subsystem_type = (
            "extend" if domain in sources_text.lower() or name in sources_text.lower() else "new"
        )
        subsystems.append(
            Subsystem(
                name=name,
                type=subsystem_type,
                responsibility=responsibility_for(domain),
                capabilities=capabilities_for(domain, matches),
                data_entities=entities_for(domain),
                dependencies=[],
            )
        )
    if not subsystems:
        subsystems = [
            Subsystem(
                name="core-service",
                type="new",
                responsibility="承载核心业务流程和领域规则",
                capabilities=["需求管理", "业务流程编排", "状态追踪"],
                data_entities=["Requirement", "Workflow"],
            )
        ]
    return subsystems
