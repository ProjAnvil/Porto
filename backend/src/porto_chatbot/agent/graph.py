"""Porto workflow graph:线性 retrieve→understand→identify→generate→evaluate,
interrupt_after 一比一替换旧 CHECKPOINTS。

节点签名 ``(state, *, config) -> partial``,agent 经 ``config["configurable"]["agent"]`` 注入。
STEPS / INTERRUPT_AFTER 作为拓扑定义的归属地驻留此处。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes import evaluate as evaluate_node
from .nodes import generate as generate_node
from .nodes import identify as identify_node
from .nodes import retrieve as retrieve_node
from .nodes import understand as understand_node
from .state import PortoAgentState

#: 5 步流水线顺序(固定)。
STEPS = ["retrieve", "understand", "identify", "generate", "evaluate"]

#: 执行到此处停,等待用户继续(等价旧 CHECKPOINTS / route 的 _EDITABLE_STEPS)。
INTERRUPT_AFTER = ["understand", "identify", "generate"]

_NODE_FNS = {
    "retrieve": retrieve_node.retrieve_knowledge,
    "understand": understand_node.understand_prd,
    "identify": identify_node.identify_subsystems,
    "generate": generate_node.generate_specs,
    "evaluate": evaluate_node.evaluate,
}


def build_workflow_graph(checkpointer):
    """编译 workflow StateGraph(线性 + interrupt_after)。checkpointer 单例由调用方注入。"""
    g = StateGraph(PortoAgentState)
    for name in STEPS:
        g.add_node(name, _NODE_FNS[name])
    g.add_edge(START, STEPS[0])
    for a, b in zip(STEPS, STEPS[1:], strict=False):
        g.add_edge(a, b)
    g.add_edge(STEPS[-1], END)
    return g.compile(checkpointer=checkpointer, interrupt_after=INTERRUPT_AFTER)
