"""WorkflowExecutor —— 后台线程用 langgraph graph 推进 workflow,每 workflow 一把锁防并发 advance。

职责:
- ``start_workflow(id)``: 起后台 daemon 线程 ``graph.invoke(initial, config)`` 跑到首个 interrupt。
- ``advance(id) -> bool``: 加锁;若 workflow 正在 running 返回 False(调用方应 409),
  否则起后台线程 ``list(graph.stream(None, config))`` 续跑到下个 interrupt。
- snapshot 重建:从 ``workflows.rag_snapshot``/``agent_snapshot`` 重建 Settings + PortoAgent,
  经 ``config["configurable"]["agent"]`` 注入 graph(不走 db —— 免受后续配置改动影响)。
- ``_project_state``: 从 ``graph.get_state(config).values`` 投影已完成步到 workflow_outputs
  (按 ``get_state().next`` 截取;保既有 produced_by;清下游 —— 等价旧 clear_outputs_after)。
"""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any

from pydantic import BaseModel

from .agent import PortoAgent
from .agent.graph import STEPS
from .agent.heuristics import infer_project_name
from .llm import LLMClient
from .logging_utils import get_component_logger
from .models.enums import WorkflowRunState
from .settings import Settings
from .vector_store import LocalVectorStore
from .workflow_store import WorkflowStore

logger = get_component_logger("workflow_executor")

#: 各 step 的产出字段(graph state values → output json)。
_STEP_OUTPUT_KEYS: dict[str, list[str]] = {
    "retrieve": ["sources"],
    "understand": ["understanding"],
    "identify": ["subsystems"],
    "generate": ["specs", "spec_results"],
    "evaluate": ["evaluation"],
}

#: F2: _STEP_OUTPUT_KEYS 的 keys 必须与 agent.graph.STEPS 一致(单一来源,防漂移)。
assert list(_STEP_OUTPUT_KEYS) == STEPS, (
    f"_STEP_OUTPUT_KEYS keys drift from STEPS: {list(_STEP_OUTPUT_KEYS)} != {STEPS}"
)


def _to_jsonable(value: Any) -> Any:
    """把 Pydantic 模型 / dict / list 递归转为 json.dumps 可序列化的值。"""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_to_jsonable(v) for v in value]
    return value


class WorkflowRunning(Exception):
    """workflow 正在 advance(worker 持 guard),PUT/PATCH 不能并发编辑。

    F3:advance 的 graph.stream 与 PUT/PATCH 的 graph.update_state 并发会丢更新
    (worker 完成节点写 checkpoint 时覆盖 update_state)。路由 catch → 409。
    """


class WorkflowExecutor:
    """后台线程用 langgraph graph 推进 workflow;每 workflow 一把锁防并发 advance。

    锁策略同旧版:guard 在 ``Thread.start()`` 之前获取、由 worker 在 ``finally`` release,
    保证两次并发 ``advance`` 不会都返回 True 并各自起 worker(跳过 checkpoint)。
    """

    def __init__(self, settings: Settings, store: WorkflowStore, graph: Any):
        self.settings = settings
        self.store = store
        self.graph = graph
        self._guards: dict[str, threading.Lock] = {}
        self._global = threading.Lock()

    # ------------------------------------------------------------------ guards

    def _guard(self, workflow_id: str) -> threading.Lock:
        with self._global:
            if workflow_id not in self._guards:
                self._guards[workflow_id] = threading.Lock()
            return self._guards[workflow_id]

    def _is_running(self, workflow_id: str) -> bool:
        return self._guard(workflow_id).locked()

    def is_any_running(self) -> bool:
        """True if any workflow's guard is currently held (a worker is mid-run).

        Used by test-fixture teardown (reset_rag_singletons) to avoid closing the
        shared sqlite checkpoint connection while a langgraph internal worker
        thread may still be doing C-level sqlite ops (which segfaults).
        """
        with self._global:
            return any(lk.locked() for lk in self._guards.values())

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
        guard = self._guard(workflow_id)
        if not guard.acquire(blocking=False):
            raise RuntimeError(f"workflow {workflow_id} already started")
        try:
            threading.Thread(
                target=self._worker, args=(workflow_id, guard, False), daemon=True
            ).start()
        except Exception:
            guard.release()
            raise

    def advance(self, workflow_id: str) -> bool:
        guard = self._guard(workflow_id)
        if not guard.acquire(blocking=False):
            return False
        try:
            threading.Thread(
                target=self._worker, args=(workflow_id, guard, True), daemon=True
            ).start()
        except Exception:
            guard.release()
            raise
        return True

    # --------------------------------------------------------------- internals

    def _worker(self, workflow_id: str, guard: threading.Lock, resume: bool) -> None:
        try:
            (self._run_advance if resume else self._run_start)(workflow_id)
        except Exception:
            logger.exception("workflow worker crashed workflow_id=%s", workflow_id)
            try:
                self.store.update_status(workflow_id, WorkflowRunState.FAILED, error="worker crashed")
            except Exception as exc:
                logger.warning("update_status FAILED failed: %s", exc)
        finally:
            guard.release()

    def _build_agent(self, row: dict[str, Any]) -> PortoAgent:
        from .api.deps import runtime_settings_from_snapshot

        rag_snap = json.loads(row["rag_snapshot"])
        agent_snap = json.loads(row["agent_snapshot"])
        runtime = runtime_settings_from_snapshot(rag_snap, agent_snap, row["top_k"])
        return PortoAgent(runtime, LocalVectorStore(runtime), LLMClient(runtime))

    @staticmethod
    def _config(workflow_id: str, agent: Any = None) -> dict:
        cfg: dict = {"configurable": {"thread_id": workflow_id}}
        if agent is not None:
            cfg["configurable"]["agent"] = agent
        return cfg

    def _run_start(self, workflow_id: str) -> None:
        row = self.store.get(workflow_id)
        if row is None:
            logger.warning("workflow not found workflow_id=%s", workflow_id)
            return
        self.store.update_status(workflow_id, WorkflowRunState.RUNNING)
        agent = self._build_agent(row)
        config = self._config(workflow_id, agent)
        initial = {
            "workflow_id": workflow_id,
            "project_name": row["project_name"] or infer_project_name(row["prd_text"]),
            "prd_text": row["prd_text"],
            "top_k": row["top_k"],
            "steps": [],
            "sources": [],
            "understanding": "",
            "subsystems": [],
            "specs": {},
            "spec_results": {},
            "evaluation": {},
        }
        try:
            self.graph.invoke(initial, config)  # retrieve→understand,停
            # 投影 + 同步状态也纳入 try:投影失败不应掩盖一次成功的 graph 运行
            # (否则会冒泡到 _worker 的 outer except,把已完成的图误标 "worker crashed")。
            self._project_state(workflow_id, config)
            self._sync_status(workflow_id, config)
        except Exception as exc:
            logger.exception("workflow start failed workflow_id=%s", workflow_id)
            self.store.update_status(workflow_id, WorkflowRunState.FAILED, error=str(exc))
            return

    def _run_advance(self, workflow_id: str) -> None:
        row = self.store.get(workflow_id)
        if row is None:
            logger.warning("workflow not found workflow_id=%s", workflow_id)
            return
        self.store.update_status(workflow_id, WorkflowRunState.RUNNING)
        agent = self._build_agent(row)
        config = self._config(workflow_id, agent)
        try:
            list(self.graph.stream(None, config))  # 续跑到下个 interrupt / END
            # 投影 + 同步状态也纳入 try(同 _run_start):投影/同步失败用真实异常标记,
            # 不让 _worker 把成功的 graph 运行误报为 "worker crashed"。
            self._project_state(workflow_id, config)
            self._sync_status(workflow_id, config)
        except Exception as exc:
            logger.exception("workflow advance failed workflow_id=%s", workflow_id)
            self.store.update_status(workflow_id, WorkflowRunState.FAILED, error=str(exc))
            return

    def _completed_steps(self, config: dict) -> list[str]:
        """按 ``get_state().next`` 算已完成步(投影到 next 之前,含)。"""
        snap = self.graph.get_state(config)
        nxt = list(snap.next or [])
        if not nxt:
            return list(STEPS)  # 到 END,全部完成
        first = nxt[0]
        end_idx = STEPS.index(first) - 1 if first in STEPS else len(STEPS) - 1
        return STEPS[: end_idx + 1] if end_idx >= 0 else []

    def _project_state(
        self,
        workflow_id: str,
        config: dict,
        *,
        produced_by_overrides: dict[str, str] | None = None,
        default_produced_by: str = "ai",
    ) -> None:
        """从 graph state 投影已完成步到 workflow_outputs。

        - 内容**未变**的步跳过(保既有 produced_by/produced_at —— 等价旧 _persist_state
          只落本次跑过的步);新步或内容变化的步才写。
        - ``produced_by_overrides`` 中的步**强制写**(如 PUT 的 edited step,即使内容
          相同也要标 user);其余步的 produced_by 取既有值,缺失才用 default。
        - 清下游:已完成步之后的产出删掉(等价旧 clear_outputs_after —— PUT 回退后下游须重算)。
        """
        values = self.graph.get_state(config).values
        completed = self._completed_steps(config)
        existing = self.store.get_outputs(workflow_id)
        overrides = produced_by_overrides or {}
        for step in completed:
            out = {
                k: _to_jsonable(values[k]) for k in _STEP_OUTPUT_KEYS.get(step, []) if k in values
            }
            if not out:
                continue
            # 附加 tool_meta(单步从 AgentStep.data;generate 聚合 any truncated)
            tool_meta = self._tool_meta_for(step, values)
            if tool_meta is not None:
                out["tool_meta"] = tool_meta
            existing_step = existing.get(step)
            forced = step in overrides
            if not forced and existing_step and existing_step["output"] == out:
                continue  # 内容未变:保留既有 produced_by/produced_at
            produced_by = overrides.get(step) or (existing_step or {}).get(
                "produced_by", default_produced_by
            )
            self.store.save_output(workflow_id, step, out, produced_by)
        # 清下游
        last = completed[-1] if completed else None
        if last is not None:
            self.store.clear_outputs_after(workflow_id, last)

    @staticmethod
    def _tool_meta_for(step: str, values: dict) -> dict | None:
        """单步:从 state.steps 找该步 AgentStep.data["tool_meta"]。
        generate:spec_results 已自带 per-subsystem tool_meta(经 _to_jsonable 转 dict),
        此处只返回 step 级聚合 {truncated: any} 供红 chip 判断。
        """
        if step == "generate":
            spec_results = values.get("spec_results") or {}
            # 生产态 spec_results 值为 SpecResult(_to_jsonable 后是 dict);
            # 防御非 dict 值(如 trivial 测试图用 str 作 stand-in)跳过,不影响 any 聚合。
            per: dict[str, dict] = {}
            for name, r in spec_results.items():
                jr = _to_jsonable(r)
                if isinstance(jr, dict):
                    per[name] = jr.get("tool_meta") or {}
            truncated = any(tm.get("truncated") for tm in per.values())
            return {"truncated": truncated}
        steps = values.get("steps") or []
        # 必须按 step 名前缀过滤:state.steps 含多步 AgentStep,只有当前步的 tool_meta
        # 才属于此 step(否则会把 understand 的红 chip 错投到 identify/evaluate)。
        # AgentStep.name 约定为 f"{step}_*"(understand_prd / identify_subsystems / ...)。
        for st in reversed(steps):  # 取该 step 最新一条
            name = getattr(st, "name", None) or (st.get("name") if isinstance(st, dict) else "")
            if not name or not name.startswith(f"{step}_"):
                continue
            data = getattr(st, "data", None) or (st.get("data") if isinstance(st, dict) else None)
            if data and "tool_meta" in data:
                return data["tool_meta"]
        return None

    def _sync_status(self, workflow_id: str, config: dict) -> None:
        snap = self.graph.get_state(config)
        status = WorkflowRunState.COMPLETED if not snap.next else WorkflowRunState.AWAITING_INPUT
        self.store.update_status(workflow_id, status, current_step=snap.values.get("current_step"))

    # ----------------------------------------------------- PUT / PATCH / recovery

    def update_step(self, workflow_id: str, step: str, body: dict[str, Any]) -> None:
        """PUT /steps/{step}:覆盖产出 + 回退到该步(用户编辑)。

        graph.update_state(as_node=step) 把图位置回退到该步之后(下游重算);投影把
        该步标记 produced_by="user",清下游。F3:acquire per-workflow guard —— advance
        进行中(worker 持 guard)raise WorkflowRunning(路由→409),避免 graph.update_state
        与 graph.stream 并发丢更新(worker 写 checkpoint 覆盖 update_state)。update_state
        不执行节点,不需要 agent。
        """
        guard = self._guard(workflow_id)
        if not guard.acquire(blocking=False):
            raise WorkflowRunning(workflow_id)
        try:
            config = self._config(workflow_id)
            self.graph.update_state(config, {**body, "current_step": step}, as_node=step)
            self._project_state(workflow_id, config, produced_by_overrides={step: "user"})
            self._sync_status(workflow_id, config)
        finally:
            guard.release()

    def update_spec(self, workflow_id: str, name: str, body: str) -> bool:
        """PATCH /specs:轻量改单个 spec(审计 + graph state)。

        F3:acquire guard —— advance 进行中 raise WorkflowRunning(路由→409),避免 graph
        同步与 stream 并发丢更新。store.update_spec 改 workflow_outputs(specs dict-merge,
        不动 produced_by/下游/status);再 best-effort 同步到 graph state(无 checkpoint ——
        如手工造的合成 workflow —— 则跳过)。
        """
        guard = self._guard(workflow_id)
        if not guard.acquire(blocking=False):
            raise WorkflowRunning(workflow_id)
        try:
            if not self.store.update_spec(workflow_id, name, body):
                return False
            config = self._config(workflow_id)
            try:
                self.graph.update_state(config, {"specs": {name: body}})
            except Exception:
                # 无 checkpoint 或 graph 不可写:审计已落 store,graph 同步跳过
                logger.warning(
                    "update_spec graph sync skipped workflow_id=%s", workflow_id, exc_info=True
                )
            return True
        finally:
            guard.release()

    # --------------------------------------------------------------- rerun

    def _next_max_turns(self, workflow_id: str) -> int:
        """ceil(current × 1.5),cap 到 tool_turn_hard_cap。"""
        row = self.store.get(workflow_id)
        snap = json.loads(row["agent_snapshot"])
        cur = int(snap.get("agent_max_tool_turns") or self.settings.agent_max_tool_turns)
        return min(math.ceil(cur * 1.5), self.settings.tool_turn_hard_cap)

    def _apply_new_max(self, workflow_id: str, new_max: int) -> None:
        self.store.update_agent_snapshot(workflow_id, {"agent_max_tool_turns": new_max})

    def rerun_step(self, workflow_id: str, step: str) -> None:
        """手动整步重跑:turn ×1.5 cap hard_cap;绕过 graph 直接调节点函数。

        撞顶(current >= cap)或正在 running → raise WorkflowRunning(路由→409)。
        """
        guard = self._guard(workflow_id)
        if not guard.acquire(blocking=False):
            raise WorkflowRunning(workflow_id)
        row = self.store.get(workflow_id)
        if row is None:
            guard.release()
            raise RuntimeError(f"workflow {workflow_id} not found")
        cur = int((json.loads(row["agent_snapshot"])).get("agent_max_tool_turns")
                  or self.settings.agent_max_tool_turns)
        if cur >= self.settings.tool_turn_hard_cap:
            guard.release()
            raise WorkflowRunning(workflow_id)  # 撞顶 → 409 引导手编
        try:
            threading.Thread(
                target=self._worker_rerun, args=(workflow_id, guard, step), daemon=True
            ).start()
        except Exception:
            guard.release()
            raise

    def _worker_rerun(self, workflow_id: str, guard: threading.Lock, step: str) -> None:
        try:
            self._run_rerun(workflow_id, step)
        except Exception:
            logger.exception("workflow rerun crashed workflow_id=%s", workflow_id)
            try:
                self.store.update_status(workflow_id, WorkflowRunState.FAILED, error="rerun crashed")
            except Exception as exc:
                logger.warning("update_status FAILED failed: %s", exc)
        finally:
            guard.release()

    def _run_rerun(self, workflow_id: str, step: str) -> None:
        from .agent.graph import _NODE_FNS

        new_max = self._next_max_turns(workflow_id)
        self._apply_new_max(workflow_id, new_max)
        self.store.update_status(workflow_id, WorkflowRunState.RUNNING)
        agent = self._build_agent(self.store.get(workflow_id))  # 含 new_max 的 snapshot
        config = self._config(workflow_id, agent)
        state = self.graph.get_state(config).values
        node_fn = _NODE_FNS[step]
        partial = node_fn(state, config=config)
        self.graph.update_state(config, partial, as_node=step)
        self._project_state(workflow_id, config, default_produced_by="ai")
        self._sync_status(workflow_id, config)

    def recover_on_startup(self) -> int:
        """启动恢复:扫 status="running" 与 "awaiting_input"。

        awaiting_input 中 pre-L2 留下的孤儿在新库里无 checkpoint,首次 advance 会崩,
        需标 interrupted(而非误导性的 awaiting_input)。

        - 有 checkpoint 且在 interrupt 处(next 非空):
          · status="awaiting_input" → 不动(正常 L2 暂停态,可继续 advance);
          · status="running" → 改 awaiting_input(崩溃在 interrupt 处,可继续);
        - 有 checkpoint 但 next 空(已到 END,如 worker 崩在 stream-end 与 sync_status 之间)
          → completed;
        - 无 checkpoint(running 崩溃前未真正跑过 / awaiting_input 的 pre-L2 孤儿 / 合成数据)
          → interrupted,保留既有 current_step。
        返回处理条数。
        """
        # 先快照要处理的 workflow —— 循环中 update_status 会改 status,边查边改会导致
        # running→awaiting_input 的行在第二轮 awaiting_input 扫描中被重复处理。
        targets: list[tuple[str, str]] = []  # (workflow_id, 原始 status)
        for status in (WorkflowRunState.RUNNING, WorkflowRunState.AWAITING_INPUT):
            rows, _ = self.store.list_workflows(status=status)
            targets.extend((r["workflow_id"], status) for r in rows)
        count = 0
        for wid, status in targets:
            count += 1
            config = self._config(wid)
            try:
                snap = self.graph.get_state(config)
            except Exception:
                logger.warning("recover get_state failed workflow_id=%s", wid, exc_info=True)
                snap = None
            has_checkpoint = bool(snap and snap.values)
            if status == WorkflowRunState.AWAITING_INPUT and has_checkpoint and snap.next:
                continue  # 正常 L2 暂停态,不动
            if snap and snap.next:
                self.store.update_status(
                    wid, WorkflowRunState.AWAITING_INPUT, current_step=snap.values.get("current_step")
                )
            elif snap and snap.values:
                # 有 checkpoint 且已到 END → completed(worker 崩在 sync_status 之前)
                self.store.update_status(
                    wid, WorkflowRunState.COMPLETED, current_step=snap.values.get("current_step")
                )
            else:
                # 无 checkpoint → interrupted,current_step=None 保留既有
                self.store.update_status(wid, WorkflowRunState.INTERRUPTED)
        return count
