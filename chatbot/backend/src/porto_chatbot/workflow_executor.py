"""WorkflowExecutor —— 后台线程推进 workflow,每 workflow 一把锁防并发 advance。

职责:
- ``start_workflow(id)``: 起后台 daemon 线程跑到第一个 checkpoint(understand)。
- ``advance(id) -> bool``: 加锁;若 workflow 正在 running 返回 False(调用方应 409),
  否则起后台线程跑到下个 checkpoint,返回 True。
- snapshot 重建:从 ``workflows.rag_snapshot``/``agent_snapshot`` 重建 Settings,
    **不走 db** —— 免受后续配置改动影响。
- ``_persist_state``: 只保存 current_step 及之前步骤的产出(state → workflow_outputs)。

不直接调 ``PortoAgent.run()``;只用其 ``.llm``/``.vector_store``/``.settings`` 作为
容器,真正的步进由 :class:`WorkflowRunner.run_to_next_checkpoint` 驱动。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from pydantic import BaseModel

from .agent import PortoAgent
from .agent.heuristics import infer_project_name
from .llm import LLMClient
from .logging_utils import get_component_logger
from .settings import Settings
from .vector_store import LocalVectorStore
from .workflow_runner import STEPS, WorkflowRunner
from .workflow_store import WorkflowStore

logger = get_component_logger("workflow_executor")

#: 各 step 的产出字段(state → output json)。
#: 在 ``_persist_state`` 中按 ``current_step`` 截取,只持久化已完成步的产出。
_STEP_OUTPUT_KEYS: dict[str, list[str]] = {
    "retrieve": ["sources"],
    "understand": ["understanding"],
    "identify": ["subsystems"],
    "generate": ["specs", "spec_results"],
    "evaluate": ["evaluation"],
}


def _to_jsonable(value: Any) -> Any:
    """把 Pydantic 模型 / dict / list 递归转为 json.dumps 可序列化的值。

    state 中的 ``sources``/``subsystems``/``spec_results`` 是 Pydantic 模型实例
    (SourceChunk/Subsystem/SpecResult),直接 ``json.dumps`` 会 TypeError。
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_to_jsonable(v) for v in value]
    return value


class WorkflowExecutor:
    """后台线程推进 workflow;每 workflow 一把锁防并发 advance。

    锁策略:
    - 每个工作流有独立的 ``guard`` 锁;``_run_async`` 在调用线程 acquire,
      worker 线程在 ``finally`` 中 release —— 从调用方返回后 guard 仍被持有,
      直到后台步进完成。
    - ``advance`` 用 ``acquire(blocking=False)`` 试探:拿不到说明正在 running,
      返回 False(调用方应返回 409 Conflict)。
    """

    def __init__(self, settings: Settings, store: WorkflowStore):
        self.settings = settings
        self.store = store
        self._guards: dict[str, threading.Lock] = {}
        self._global = threading.Lock()

    # ------------------------------------------------------------------ guards

    def _guard(self, workflow_id: str) -> threading.Lock:
        with self._global:
            if workflow_id not in self._guards:
                self._guards[workflow_id] = threading.Lock()
            return self._guards[workflow_id]

    def _is_running(self, workflow_id: str) -> bool:
        """guard 被持有 = 正在跑。``Lock.locked()`` 可被任意线程查询。"""
        return self._guard(workflow_id).locked()

    def wait(self, workflow_id: str, timeout: float = 30.0) -> None:
        """测试用:轮询等当前后台任务结束。"""
        end = time.time() + timeout
        while time.time() < end:
            if not self._is_running(workflow_id):
                return
            time.sleep(0.05)
        raise TimeoutError(f"workflow {workflow_id} still running after {timeout}s")

    # ------------------------------------------------------------- public API

    def start_workflow(self, workflow_id: str) -> None:
        """启动首个后台步进(retrieve → understand)。

        用于刚创建的 workflow(``status="created"``);不做 running 检查 ——
        创建后第一次启动不应被 409 拦。
        """
        self._run_async(workflow_id)

    def advance(self, workflow_id: str) -> bool:
        """推进到下个 checkpoint。

        返回:
            True: 已接受(起后台线程)。
            False: 该 workflow 正在 running,调用方应返回 409。
        """
        guard = self._guard(workflow_id)
        if not guard.acquire(blocking=False):
            return False
        guard.release()
        self._run_async(workflow_id)
        return True

    # --------------------------------------------------------------- internals

    def _run_async(self, workflow_id: str) -> None:
        """acquire guard(调用线程),起 daemon worker。"""
        guard = self._guard(workflow_id)
        guard.acquire()
        t = threading.Thread(target=self._worker, args=(workflow_id, guard), daemon=True)
        t.start()

    def _worker(self, workflow_id: str, guard: threading.Lock) -> None:
        try:
            self._run_sync(workflow_id)
        except Exception:
            logger.exception("workflow worker crashed workflow_id=%s", workflow_id)
            try:
                self.store.update_status(workflow_id, "failed", error="worker crashed")
            except Exception:
                pass
        finally:
            guard.release()

    def _run_sync(self, workflow_id: str) -> None:
        row = self.store.get(workflow_id)
        if row is None:
            logger.warning("workflow not found workflow_id=%s", workflow_id)
            return
        self.store.update_status(workflow_id, "running")
        # snapshot 重建 Settings(不走 db)——后续配置改动不影响在途 workflow。
        # 延迟导入避免顶层循环依赖(api.deps 引入 supervisor/health 等重组件)。
        from .api.deps import runtime_settings_from_snapshot

        rag_snap = json.loads(row["rag_snapshot"])
        agent_snap = json.loads(row["agent_snapshot"])
        runtime = runtime_settings_from_snapshot(rag_snap, agent_snap, row["top_k"])
        agent = PortoAgent(runtime, LocalVectorStore(runtime), LLMClient(runtime))
        state = self._rebuild_state(row)
        try:
            state = WorkflowRunner.run_to_next_checkpoint(agent, state)
        except Exception as exc:
            logger.exception("workflow step failed workflow_id=%s", workflow_id)
            self.store.update_status(workflow_id, "failed", error=str(exc))
            return
        # 落库:新跑过的步的产出(仅 current_step 及之前)
        self._persist_state(workflow_id, state)
        self.store.update_status(
            workflow_id,
            state.get("status", "running"),
            current_step=state.get("current_step"),
        )

    def _rebuild_state(self, row: dict[str, Any]) -> dict[str, Any]:
        """从 db 行 + 已有 outputs 重建 WorkflowRunner 可消费的 state。"""
        workflow_id = row["workflow_id"]
        outs = self.store.get_outputs(workflow_id)
        state: dict[str, Any] = {
            "workflow_id": workflow_id,
            "project_name": row["project_name"] or infer_project_name(row["prd_text"]),
            "prd_text": row["prd_text"],
            "top_k": row["top_k"],
            "current_step": row["current_step"],
            "steps": [],
            "sources": [],
            "understanding": "",
            "subsystems": [],
            "specs": {},
            "spec_results": {},
            "evaluation": {},
        }
        # 把已保存步的产出回填到 state,让 runner 从正确的上下文继续
        for _step, data in outs.items():
            out = data["output"]
            for k, v in out.items():
                state[k] = v
        return state

    def _persist_state(self, workflow_id: str, state: dict[str, Any]) -> None:
        """只持久化"已完成步"的产出:current_step 及之前的步。

        避免把还在跑的或未来步的旧数据落库。产出经 ``_to_jsonable`` 转换,
        确保 Pydantic 模型(SourceChunk/Subsystem/SpecResult)可被 json.dumps。
        """
        cur = state.get("current_step")
        if cur in STEPS:
            end_idx = STEPS.index(cur)
        else:
            # current_step 异常(不在 STEPS 中)——保守地持久化全部
            end_idx = len(STEPS) - 1
        for step in STEPS[: end_idx + 1]:
            out = {
                k: _to_jsonable(state[k])
                for k in _STEP_OUTPUT_KEYS.get(step, [])
                if k in state
            }
            if out:
                self.store.save_output(workflow_id, step, out, "ai")
