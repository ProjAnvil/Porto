from __future__ import annotations


def retrieve_knowledge(state, *, config):
    agent = config["configurable"]["agent"]
    agent.logger.info("step retrieve_knowledge start workflow_id=%s", state.get("workflow_id"))
    agent.vector_store.ensure_index()
    query = f"{state['project_name']}\n{state['prd_text'][:2000]}"
    sources = agent.vector_store.search(query, top_k=state.get("top_k"))
    agent.logger.info(
        "step retrieve_knowledge finish workflow_id=%s sources=%s",
        state.get("workflow_id"),
        len(sources),
    )
    return {
        "sources": sources,
        "current_step": "retrieve",
        **agent._step(
            "retrieve_knowledge",
            f"检索到 {len(sources)} 个知识库片段",
            {"source_paths": [s.path for s in sources]},
        ),
    }
