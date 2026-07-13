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
    - 每个工作流有独立的 ``guard`` 锁;调用线程(acquire 非阻塞)持有 guard
      **跨过** ``Thread.start()``,worker 线程在 ``finally`` 中 release ——
      从调用方返回后 guard 仍被持有,直到后台步进完成。
      guard 在 ``Thread.start()`` 期间绝不被 release,避免两个并发 ``advance``
      之间出现 TOCTOU 窗口(否则两者都返回 True 并各自起一个 worker,跳过 checkpoint)。
    - ``advance`` 用 ``acquire(blocking=False)`` 试探:拿不到说明正在 running,
      返回 False(调用方应返回 409 Conflict)。
    - 若 ``Thread.start()`` 失败,调用线程负责 release guard(worker 从未启动,
      无法在 ``finally`` 里 release)。
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

        用于刚创建的 workflow(``status="created"``)。若该 workflow 已在
        running(guard 已被持有)——编程错误——抛 ``RuntimeError``。
        """
        guard = self._guard(workflow_id)
        if not guard.acquire(blocking=False):
            raise RuntimeError(f"workflow {workflow_id} already started")
        try:
            threading.Thread(
                target=self._worker, args=(workflow_id, guard), daemon=True
            ).start()
        except Exception:
            guard.release()
            raise

    def advance(self, workflow_id: str) -> bool:
        """推进到下个 checkpoint。

        返回:
            True: 已接受(已起后台线程)。
            False: 该 workflow 正在 running,调用方应返回 409。

        锁策略:guard 在 ``Thread.start()`` 之前获取、之后才(由 worker)release,
        保证两次并发 ``advance`` 不会都返回 True 并各自起 worker(跳过 checkpoint)。
        """
        guard = self._guard(workflow_id)
        if not guard.acquire(blocking=False):
            return False
        try:
            threading.Thread(
                target=self._worker, args=(workflow_id, guard), daemon=True
            ).start()
        except Exception:
            guard.release()
            raise
        return True

    # --------------------------------------------------------------- internals

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
        # 记录本次推进前的 current_step;_persist_state 只落 THIS invocation 实际跑过的步,
        # 不重写更早的(用户可能已 produced_by="user" 编辑过,保留审计轨迹)。
        before_step = row["current_step"]
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
        # 落库:仅本次实际跑过的步(before_step+1 .. current_step)
        self._persist_state(workflow_id, state, before_step)
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

    def _persist_state(
        self,
        workflow_id: str,
        state: dict[str, Any],
        before_step: str | None,
    ) -> None:
        """只持久化**本次推进实际跑过**的步的产出。

        步骤范围:``STEPS[index(before_step)+1 : index(current_step)+1]`` —— 即
        ``before_step`` 之后的步到 ``current_step`` 之间的步,这些才是 runner 刚执行的。
        更早的步是从 db 经 ``_rebuild_state`` 回填的,保留其 stored ``produced_by``
        (用户可能编辑过,不应被这里统一覆盖为 ``"ai"``)。

        产出经 ``_to_jsonable`` 转换,确保 Pydantic 模型
        (SourceChunk/Subsystem/SpecResult)可被 json.dumps。
        """
        cur = state.get("current_step")
        # end_idx = current_step 在 STEPS 中的位置;不在则保守地取到末尾。
        if cur in STEPS:
            end_idx = STEPS.index(cur)
        else:
            end_idx = len(STEPS) - 1
        # start_idx = before_step 之后的第一个步;before 缺失/不在 STEPS 时从 0 起。
        if before_step in STEPS:
            start_idx = STEPS.index(before_step) + 1
        else:
            start_idx = 0
        # start_idx 不能超过 end_idx + 1(至少是空切片)。
        start_idx = min(start_idx, end_idx + 1)
        for step in STEPS[start_idx : end_idx + 1]:
            out = {
                k: _to_jsonable(state[k])
                for k in _STEP_OUTPUT_KEYS.get(step, [])
                if k in state
            }
            if out:
                self.store.save_output(workflow_id, step, out, "ai")
