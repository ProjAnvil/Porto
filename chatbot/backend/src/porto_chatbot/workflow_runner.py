"""WorkflowRunner —— 纯状态机,从 current_step 的下一步跑到下个 checkpoint(或 completed)。

设计要点:
- 不碰 sqlite / 线程,产出写入 state,调用方(WorkflowExecutor,Task 6)负责落库。
- 复用现有 ``agent/nodes/*`` 的节点函数(签名 ``node(agent, state) -> state``)。
- 节点分派采用运行时 ``getattr(_NODE_MODULES[step], _NODE_FN[step])``,而非预绑定
  字典 —— 这样测试 ``monkeypatch`` 模块属性(如 ``retrieve_node.retrieve_knowledge``)
  才能在调用时生效。预绑定字典会在 import 时捕获原始引用,导致 mock 失效。
"""

from __future__ import annotations

from typing import Any

from .agent.nodes import evaluate as evaluate_node
from .agent.nodes import generate as generate_node
from .agent.nodes import identify as identify_node
from .agent.nodes import retrieve as retrieve_node
from .agent.nodes import understand as understand_node

#: 5 步流水线顺序(固定)。
STEPS = ["retrieve", "understand", "identify", "generate", "evaluate"]

#: 用户可编辑产出的 checkpoint 步骤(执行到此处停,等待用户继续)。
CHECKPOINTS = {"understand", "identify", "generate"}

# 节点函数签名:node(agent, state) -> state;各节点内部已 self._with_step(...)。
# 用 (模块, 函数名) 元组而非直接引用,让测试 monkeypatch 模块属性生效。
_NODE_MODULES = {
    "retrieve": retrieve_node,
    "understand": understand_node,
    "identify": identify_node,
    "generate": generate_node,
    "evaluate": evaluate_node,
}
_NODE_FN = {
    "retrieve": "retrieve_knowledge",
    "understand": "understand_prd",
    "identify": "identify_subsystems",
    "generate": "generate_specs",
    "evaluate": "evaluate",
}


class WorkflowRunner:
    """纯状态机:从 ``current_step`` 的下一步跑到下个 checkpoint(或 completed)。

    ``state`` 是 :class:`PortoAgentState` 外加一个 ``current_step`` 键(以及本 runner
    维护的 ``status`` 键)。TypedDict ``total=False`` 允许这些扩展键存在。
    """

    @staticmethod
    def run_to_next_checkpoint(
        agent: Any, state: dict[str, Any]
    ) -> dict[str, Any]:
        """从 ``state["current_step"]`` 的下一步开始跑,直到遇到 checkpoint 或全部跑完。

        - 若 ``current_step`` 为 ``None`` 或不在 :data:`STEPS` 中,从第 0 步(retrieve)开始。
        - 每跑完一步写入 ``state["current_step"] = step``。
        - 遇到 :data:`CHECKPOINTS` 中的步骤,设 ``status="awaiting_input"`` 并返回。
        - 全部跑完设 ``status="completed"`` 并返回。
        """
        current = state.get("current_step")
        start_idx = STEPS.index(current) + 1 if current in STEPS else 0
        for step in STEPS[start_idx:]:
            # 运行时查找:让测试对模块属性的 monkeypatch 生效。
            fn = getattr(_NODE_MODULES[step], _NODE_FN[step])
            state = fn(agent, state)
            state["current_step"] = step
            if step in CHECKPOINTS:
                state["status"] = "awaiting_input"
                return state
        state["status"] = "completed"
        return state
