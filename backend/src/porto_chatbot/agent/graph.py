"""Porto workflow graph:retrieve→understand→identify→generate(spec 子图 fan-out)。

Task 9:``evaluate`` 节点移除;``generate`` 步改为 spec evaluator-optimizer 子图,
经 ``Send`` fan-out 对每个子系统启动一个子图实例,各实例的 ``spec_results``/``specs``
经 ``_dict_merge`` 合并。节点名保持 ``"generate"`` —— workflow_store SQL
(``WHERE step_name='generate'``)+ 前端 ``outputs.generate`` 不变(B2 A 方案)。

节点签名 ``(state, *, config) -> partial``,agent 经 ``config["configurable"]["agent"]`` 注入。
STEPS / INTERRUPT_AFTER 作为拓扑定义的归属地驻留此处。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from ..specs.subgraph import build_spec_subgraph
from .nodes import generate as generate_node
from .nodes import identify as identify_node
from .nodes import retrieve as retrieve_node
from .nodes import understand as understand_node
from .state import PortoAgentState

#: 4 步流水线顺序(固定)。generate = spec 子图(Send fan-out);evaluate 已移除(Task 9)。
STEPS = ["retrieve", "understand", "identify", "generate"]

#: 执行到此处停,等待用户继续(等价旧 CHECKPOINTS)。generate 停 → 用户审计 spec。
INTERRUPT_AFTER = ["understand", "identify", "generate"]

#: rerun_step 用的节点函数注册表(去 evaluate,Task 10 清理)。
#: 与 build_workflow_graph 的 ``add_node`` 保持一致 —— retrieve/understand/identify
#: 直调节点函数;generate 在 STEPS 中但不在本表(spec 子图不能绕过父图 dispatch 单独
#: rerun —— 父图经 Send fan-out 启动子图,rerun 路径无法重建该拓扑)。rerun generate
#: 会在 ``_NODE_FNS[step]`` KeyError,被 _worker_rerun 捕获标 FAILED。后续若需支持,
#: 改为 rerun 时按子系统 fan-out 重跑子图。
_NODE_FNS = {
    "retrieve": retrieve_node.retrieve_knowledge,
    "understand": understand_node.understand_prd,
    "identify": identify_node.identify_subsystems,
}


def build_workflow_graph(checkpointer):
    """编译 workflow StateGraph。

    拓扑:``retrieve → understand → identify --(dispatch_specs)--> generate(子图) --> END``。

    ``identify → generate`` 是 ``add_conditional_edges``:``dispatch_specs`` 返回
    ``[Send("generate", {...}) for sub in subsystems]``,LangGraph 对每个子系统 fan-out
    一个 ``generate`` 子图实例。``generate`` 节点本身是 ``build_spec_subgraph()`` 编译产物。
    """
    g = StateGraph(PortoAgentState)
    g.add_node("retrieve", retrieve_node.retrieve_knowledge)
    g.add_node("understand", understand_node.understand_prd)
    g.add_node("identify", identify_node.identify_subsystems)
    g.add_node("generate", build_spec_subgraph())  # B2 A 方案:节点名 generate,内部是子图
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "understand")
    g.add_edge("understand", "identify")
    g.add_conditional_edges("identify", generate_node.dispatch_specs, ["generate"])
    g.add_edge("generate", END)
    return g.compile(checkpointer=checkpointer, interrupt_after=INTERRUPT_AFTER)
