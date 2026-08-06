from __future__ import annotations

from ...models.enums import QueryTransformStrategy
from ...query_transform import retrieve_with_transform
from ._prd import read_prd_text


def retrieve_knowledge(state, *, config):
    agent = config["configurable"]["agent"]
    agent.logger.info("step retrieve_knowledge start workflow_id=%s", state.get("workflow_id"))
    agent.vector_store.ensure_index()
    # Task 7:经 file_service 分页读前 5 页(避开 6000 字截断);text 路由自动回退。
    prd_text = read_prd_text(state, getattr(agent, "file_service", None))
    query = f"{state['project_name']}\n{prd_text[:2000]}"

    # Task 8:workflow 检索走 retrieve_with_transform；strategy 从 agent.settings 取
    # （创建 workflow 时快照进 settings）。getattr 防御老快照缺该字段。
    strategy = getattr(agent.settings, "workflow_query_transform_strategy", QueryTransformStrategy.NONE)
    llm = getattr(agent, "llm", None)
    result = retrieve_with_transform(
        query, strategy, agent.vector_store, agent.settings, llm, top_k=state.get("top_k")
    )
    sources = result.chunks

    step_meta: dict = {"source_paths": [s.path for s in sources]}
    if result.degraded:
        step_meta["transform_degraded"] = result.degrade_reason

    agent.logger.info(
        "step retrieve_knowledge finish workflow_id=%s sources=%s degraded=%s",
        state.get("workflow_id"),
        len(sources),
        result.degraded,
    )
    return {
        "sources": sources,
        "current_step": "retrieve",
        **agent._step(
            "retrieve_knowledge",
            f"检索到 {len(sources)} 个知识库片段",
            step_meta,
        ),
    }
