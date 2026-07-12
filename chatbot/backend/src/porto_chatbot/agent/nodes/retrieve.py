from __future__ import annotations

from ..state import PortoAgentState


def retrieve_knowledge(agent, state: PortoAgentState) -> PortoAgentState:
    agent.logger.info("step retrieve_knowledge start workflow_id=%s", state["workflow_id"])
    agent.vector_store.ensure_index()
    query = f"{state['project_name']}\n{state['prd_text'][:2000]}"
    sources = agent.vector_store.search(query, top_k=state.get("top_k"))
    agent.logger.info(
        "step retrieve_knowledge finish workflow_id=%s sources=%s",
        state["workflow_id"],
        len(sources),
    )
    return agent._with_step(
        {**state, "sources": sources},
        "retrieve_knowledge",
        f"检索到 {len(sources)} 个知识库片段",
        {"source_paths": [s.path for s in sources]},
    )
