from __future__ import annotations

from ._prd import read_prd_text


def retrieve_knowledge(state, *, config):
    agent = config["configurable"]["agent"]
    agent.logger.info("step retrieve_knowledge start workflow_id=%s", state.get("workflow_id"))
    agent.vector_store.ensure_index()
    # Task 7:经 file_service 分页读前 5 页(避开 6000 字截断);text 路由自动回退。
    prd_text = read_prd_text(state, getattr(agent, "file_service", None))
    query = f"{state['project_name']}\n{prd_text[:2000]}"
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
