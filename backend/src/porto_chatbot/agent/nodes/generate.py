"""generate 步入口:经 ``Send`` 把每个子系统 fan-out 到 spec 子图。

B2 A 方案:节点名保持 ``"generate"``,spec 子图作为该节点的实现(子图 compile 产物直接
``add_node``)。``dispatch_specs`` 是 ``identify`` → ``generate`` 的 conditional_edges
路由函数:返回 ``[Send("generate", {...}) for sub in subsystems]``,LangGraph 对每个
子系统启动一个子图实例,各实例的 ``spec_results`` / ``specs`` 经 ``_dict_merge`` 合并。

**序列化约束**:Send payload 会被父图 checkpoint 序列化(msgpack),故只含可序列化字段
(``sub`` / ``prd_file_id`` / ``ctx_state``)。agent 的运行时引用(backend/llm/settings/
vector_store/critic_llm/_spec_sema)经 ``config["configurable"]["agent"]`` 传递 ——
父图与子图共享 config,子图 ``init_spec`` 从中取出填入子图 state(子图无独立 checkpointer,
ctx_* 只活在本次执行期间)。
"""

from __future__ import annotations

from langgraph.types import Send


def dispatch_specs(state, *, config):
    """``identify`` → ``generate`` 的 Send fan-out 路由。

    返回 ``[Send("generate", {...}) for sub in subsystems]``。Send payload 只含
    可序列化字段:agent 运行时对象不放入(会被 checkpoint 序列化失败),由子图
    ``init_spec`` 经 ``config["configurable"]["agent"]`` 取并填入子图 state。
    """
    agent = config["configurable"]["agent"]
    subs = state["subsystems"]
    agent.logger.info(
        "dispatch_specs fan-out workflow_id=%s subsystems=%s",
        state["workflow_id"],
        len(subs),
    )
    # Send payload 只含可序列化字段。prd_file_id 不单独放入(在 ctx_state 里);
    # 它是 last_value channel,fan-out 多实例同时写会冲突(InvalidUpdateError)。
    return [
        Send("generate", {"sub": sub, "ctx_state": {**state}})
        for sub in subs
    ]
