# LangGraph Orchestration (L2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把手写的 `WorkflowRunner` 状态机换成 langgraph `StateGraph` 编排层(interrupt + SqliteSaver + update_state),用 checkpointer 替换手写 checkpoint / advance / PUT 回退 / 崩溃恢复;API 契约不变,删掉 `_rebuild_state` / `WorkflowRunner`。

**Architecture:** 一条线性图 `retrieve→understand→identify→generate→evaluate`,`interrupt_after=["understand","identify","generate"]` 一比一替换 `CHECKPOINTS`;`SqliteSaver`(`~/.porto` 下独立 db)持久化 graph state,`thread_id=workflow_id`,`agent` 经 `RunnableConfig.configurable` 注入;`WorkflowExecutor` 改为 `graph.invoke`/`stream(None)`/`update_state`,节点签名从 `(agent, state)→全量 state` 改为 `(state, *, config)→partial dict`;`WorkflowStore` 保留并瘦身为业务视图(从 `graph.get_state().values` 投影到 `workflow_outputs`,按 `get_state().next` 截取已完成步)。

**Tech Stack:** Python 3.14(venv)、langgraph 1.2.9、langgraph-checkpoint-sqlite 3.1.0、langchain-core 1.5.1、pytest、ruff。

## Global Constraints

- **API 契约不变(D4)**:7 个 workflow endpoints(`POST /workflows`、`POST /workflows/upload`、`GET /workflows`、`GET /workflows/{id}`、`POST /advance`、`PUT /steps/{step}`、`PATCH /specs`、`DELETE`)对前端契约不变 —— 状态码、响应体形态、`produced_by`/`produced_at` 审计字段、`_EDITABLE_STEPS={"understand","identify","generate"}` 全部保留。`test_workflow_api.py` 必须保持绿(回归基线)。
- **持久化双层(D6)**:langgraph `SqliteSaver` 管 graph state/resume(`{data_dir}/langgraph_checkpoints.sqlite`,独立于 `workflows.sqlite3`);`WorkflowStore` 保留 `workflows` + `workflow_outputs` 两表(list/filter/GET/审计)。**不**把 graph state 整体塞进 `workflows` 表。
- **保持 sync(D3)**:langgraph sync API + 现有 threading;`WorkflowExecutor` 的 per-workflow `guard` 锁 + daemon 线程保留(langgraph 不管"同一 workflow 不能并发 advance")。`advance` 非阻塞 acquire 失败 → 返回 False → 路由 409。
- **降级保留(D5)**:`agent.llm.enabled=False` 时各节点走现有 fallback(understand 正则/模板、identify `DOMAIN_HINTS`、generate `render_template_spec`、evaluate 规则给分)。graph 是纯编排,不依赖 LLM,照常推进到 interrupt/END。
- **检索保留 llama-index(D1)**:`retrieve` 节点的 `vector_store.search` 不动;`chat` 路由不做 graph(D2)。
- **agent 注入**:`config = {"configurable": {"thread_id": workflow_id, "agent": PortoAgent}}`;agent 含从 snapshot 重建的 settings/llm/vector_store/critic_llm(`runtime_settings_from_snapshot` 保留)。
- **配置零改动**:`settings.py`、`llm/`、`llama-index` 检索层、8 处 LLM 调用方零改动(L1 已合入)。
- **Python 解释器**:backend venv 未默认激活,`python` 不在 PATH。**下文所有 `python` / `pytest` / `ruff` 命令一律指 `./.venv/bin/python`**(在 `backend/` 下执行),如 `cd backend && ./.venv/bin/python -m pytest tests/...`、`./.venv/bin/python -m ruff check src tests`。
- **代码风格**:ruff line-length 100、target py312;testpaths=["tests"]、pythonpath=["src"]。
- **LLM 测试**:`backend/.env` 的 `LANGCHAIN_API_KEY`/`BASE_URL` 实测为空(L1 发现),单元/集成测试一律走降级路径或 mock,**不需要真 key**。
- **设计文档**:`docs/superpowers/specs/2026-07-24-langchain-langgraph-migration-design.md` §6 = L2 设计、§3 = D1–D10。
- **分支**:从 `main` 开 `feat/langgraph-orchestration-l2`。

## 对设计 §6.2 的一处精炼(实施时遵循,非偏离意图)

设计 §6.2 把 `status` 列为 graph state 字段(last-write-wins)。实施时 **`status` 不进 `PortoAgentState`**:它是 checkpoint 位置的函数(`get_state().next` 为空 → `completed`;非空 → `awaiting_input`;节点异常 → executor 写 `failed`),由 executor 派生并落入 `workflows.status` 表。让 graph state 持有 `status` 会与 `workflows` 表冗余、且可能在崩溃时漂移。`current_step` 仍由各节点写入 state(投影 + 状态同步读它)。这条精炼在 Task 2 落地,后续 task 一致遵循。

## Scope

本 plan 覆盖设计文档 **阶段 0(L2 spike)+ 阶段 2(L2)**。L1 已合入(`main` commit 6187a9b);L3(spec 子图 + Send map-reduce)依赖 L2,**另写 plan**。本 plan 结束时:编排层是 langgraph `StateGraph`,`WorkflowRunner` 删除,全量测试绿,API 契约不变,可独立 ship。

---

## File Structure

| 文件 | 职责 | 本 plan 动作 |
|---|---|---|
| `backend/src/porto_chatbot/agent/state.py` | `PortoAgentState` TypedDict | Modify:加 `current_step`;`steps` 注解 `operator.add`,`specs`/`spec_results` 注解 dict-merge reducer |
| `backend/src/porto_chatbot/agent/agent.py` | `PortoAgent` 容器 | Modify:`_with_step` → `_step`,返回 partial `{"steps":[AgentStep]}` |
| `backend/src/porto_chatbot/agent/nodes/{retrieve,understand,identify,generate,evaluate}.py` | 5 节点 | Modify:签名 `(agent, state)->全量` → `(state, *, config)->partial`;`agent` 从 config 取;返回 partial(含 `current_step`) |
| `backend/src/porto_chatbot/agent/graph.py` | **NEW**:`STEPS`/`INTERRUPT_AFTER` + `build_workflow_graph(checkpointer)` | Create |
| `backend/src/porto_chatbot/workflow_runner.py` | 旧状态机 | **Delete**(Task 4,executor 切换后) |
| `backend/src/porto_chatbot/workflow_executor.py` | 后台线程 + 编排 + 投影 | **重写内部**:删 `_rebuild_state`;`_run_start`/`_run_advance` 走 invoke/stream;`_project_state`(从 get_state 投影,保 produced_by,清下游);`_sync_status`;新增 `update_step`/`update_spec`/`recover_on_startup` |
| `backend/src/porto_chatbot/workflow_store.py` | 业务视图 | Modify:删 `mark_running_interrupted_on_startup`(移到 executor);其余(save_output/get_outputs/clear_outputs_after/update_spec/delete)不动 |
| `backend/src/porto_chatbot/api/deps.py` | 单例工厂 | Modify:加 `get_checkpointer`/`get_workflow_graph`;`get_workflow_executor` 传 graph;`reset_rag_singletons` 关 sqlite conn |
| `backend/src/porto_chatbot/api/routes/workflow.py` | 7 endpoints | Modify:`PUT /steps` 调 `executor.update_step`;`PATCH /specs` 调 `executor.update_spec`;契约不变 |
| `backend/src/porto_chatbot/api/app.py` | lifespan | Modify:启动调 `executor.recover_on_startup` 替代 `store.mark_running_interrupted_on_startup` |
| `backend/tests/test_langgraph_orchestration_spike.py` | L2 spike | Create |
| `backend/tests/test_agent_graph.py` | graph 拓扑/interrupt/update_state | Create(替代 test_workflow_runner.py) |
| `backend/tests/test_workflow_runner.py` | 旧 runner 测试 | **Delete**(Task 4) |
| `backend/tests/test_workflow_executor.py` | executor 测试 | Modify:注入 trivial 测试 graph + 临时 checkpointer,改测 invoke/stream/project/update_step/recover |
| `backend/tests/test_workflow_store.py` | store 测试 | Modify:删 `mark_running_interrupted_on_startup` 的 2 测试 |
| `backend/tests/test_workflow_startup_recovery.py` | 启动恢复 | 保持绿(无 checkpoint→interrupted);新增 at-interrupt→awaiting 用例 |
| `backend/tests/test_agent.py` | evaluate 节点测试 | Modify:`evaluate(agent, state)` → `evaluate(state, config=...)` |

---

## Known plan-mandated middle states(reviewer 勿判为回归)

- **Task 3 之后、Task 4 之前**:`test_workflow_executor.py` + `test_workflow_api.py` 会 **RED** —— 节点签名改成 `(state, config)` 后,仍在用旧 runner 的 executor(`WorkflowRunner.run_to_next_checkpoint` 以 `fn(agent, state)` 调节点)会崩。这是预期的中间状态,Task 4 把 executor 切到 graph 后转绿。每个 task review 只看本 task 新增测试 + 实现 + "本 task 应转绿的预存测试"。
- **Task 3 之后**:`test_workflow_runner.py` RED(测的是即将删除的 runner);Task 4 直接删除该文件。
- Task 4 / Task 5 各自落地后,对应预存测试须转绿(见各 task 的"Expected:转绿"步)。

---

## Tasks

### Task 1: spike — L2 langgraph 编排原语

**目的**:把 L2 依赖的 5 个 langgraph 行为钉死成自动化测试(interrupt_after 暂停、stream(None) 续跑、update_state(as_node=) 回退并重算下游、configurable 注入 agent、Pydantic 模型过 SqliteSaver 往返、多 workflow 并发不冲突)。结论填到 §Spike Conclusions。

> **预研已跑(controller,非 implementer)**:langgraph 1.2.9 下这 5 项均已验证通过(见 §Spike Conclusions 预填)。本 task 把预研脚本固化成正式测试,implementer 按下方代码落地并确认全绿。

**Files:**
- Create: `backend/tests/test_langgraph_orchestration_spike.py`

**Interfaces:**
- Produces: 无产品代码;产出是"langgraph 行为已验证"的结论,供 Task 3–5 实现直接依据。

- [ ] **Step 1: 写 spike 测试**

`backend/tests/test_langgraph_orchestration_spike.py`:

```python
"""L2 spike: langgraph 编排原语(interrupt / resume / update_state rewind / config 注入 / Pydantic 往返 / 并发)。

结论见 plan §Spike Conclusions。这些行为是 Task 3–5(graph + executor)的实现依据。
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Annotated, TypedDict

import operator
from pydantic import BaseModel
from langgraph.graph import START, END, StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver


def _dict_merge(left: dict, right: dict) -> dict:
    return {**(left or {}), **(right or {})}


class _Widget(BaseModel):
    name: str
    qty: int


class _S(TypedDict, total=False):
    widgets: Annotated[list[_Widget], operator.add]
    specs: Annotated[dict, _dict_merge]
    current_step: str
    seen_agent: str


def _saver(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "spike.sqlite3"), check_same_thread=False)
    sv = SqliteSaver(conn)
    sv.setup()
    return sv


# ---- ① interrupt_after 暂停 + stream(None) 续跑 + ② Pydantic 过 checkpoint 往返 ----
def test_interrupt_then_resume_and_pydantic_roundtrip(tmp_path):
    consumed = {}

    def node_a(state):
        # append a Pydantic model (跨 checkpoint 后须仍是 _Widget,不能降级成 dict)
        return {"widgets": [_Widget(name="a", qty=1)], "current_step": "a"}

    def node_b(state):
        w = state["widgets"][0]
        consumed["type"] = type(w).__name__
        consumed["name"] = w.name  # 属性访问 —— 仅当重建为 _Widget 才成立
        return {"specs": {"x": "b"}, "current_step": "b"}

    g = StateGraph(_S)
    g.add_node("a", node_a)
    g.add_node("b", node_b)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    graph = g.compile(checkpointer=_saver(tmp_path), interrupt_after=["a"])
    cfg = {"configurable": {"thread_id": "t1"}}

    graph.invoke({"widgets": [], "specs": {}}, cfg)
    st = graph.get_state(cfg)
    assert list(st.next) == ["b"]                      # 暂停在 a 之后
    assert st.values["current_step"] == "a"
    assert [type(w).__name__ for w in st.values["widgets"]] == ["_Widget"]

    chunks = list(graph.stream(None, cfg))             # 续跑 a 之后 → b → END
    st2 = graph.get_state(cfg)
    assert list(st2.next) == []                        # 到 END
    assert consumed["type"] == "_Widget"               # Pydantic 过 checkpoint 往返成功
    assert consumed["name"] == "a"
    assert st2.values["specs"] == {"x": "b"}


# ---- ③ update_state(as_node=) 回退并重算下游 ----
def test_update_state_rewinds_and_reruns_downstream(tmp_path):
    order: list[str] = []

    def mk(name):
        def fn(state):
            order.append(name)
            return {"current_step": name}
        return fn

    g = StateGraph(_S)
    for n in ("a", "b", "c"):
        g.add_node(n, mk(n))
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", END)
    graph = g.compile(checkpointer=_saver(tmp_path), interrupt_after=["a", "b"])
    cfg = {"configurable": {"thread_id": "t2"}}

    graph.invoke({}, cfg)                              # 跑 a,停在 a 后
    assert order == ["a"]
    list(graph.stream(None, cfg))                      # 跑 b,停在 b 后
    assert order == ["a", "b"]

    graph.update_state(cfg, {"current_step": "a"}, as_node="a")  # 回退到 a 之后
    assert list(graph.get_state(cfg).next) == ["b"]
    order.clear()
    list(graph.stream(None, cfg))                      # 必须重跑 b(下游重算)
    assert order == ["b"], f"回退后应重跑 b,实际 {order}"


# ---- ④ configurable 注入 agent(节点从 config 取对象)----
def test_configurable_agent_injection(tmp_path):
    def node(state, *, config):                        # langgraph 1.x: (state, config)
        return {"seen_agent": config["configurable"]["agent"]}

    g = StateGraph(_S)
    g.add_node("n", node)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    graph = g.compile(checkpointer=_saver(tmp_path))
    cfg = {"configurable": {"thread_id": "t3", "agent": "AGENT_OBJ"}}
    graph.invoke({}, cfg)
    assert graph.get_state(cfg).values["seen_agent"] == "AGENT_OBJ"


# ---- ⑤ 多 workflow(不同 thread_id)在共享 SqliteSaver 上并发不冲突 ----
def test_shared_saver_concurrent_threads(tmp_path):
    def node(state):
        return {"specs": {"k": "v"}}
    g = StateGraph(_S)
    g.add_node("n", node)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    saver = _saver(tmp_path)                           # 单 saver,多 thread_id
    graph = g.compile(checkpointer=saver)

    errors: list[BaseException] = []

    def run(tid):
        try:
            graph.invoke({}, {"configurable": {"thread_id": tid}})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"t{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], f"共享 saver 并发出错: {errors}"
```

- [ ] **Step 2: 运行**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_langgraph_orchestration_spike.py -v`
Expected: **5 passed**。若 ③ 的 `update_state(as_node=)` 重跑断言失败(下游未重算),记录实际行为到 §Spike Conclusions —— L2 的 PUT 回退退化为"清下游 + 重跑全图"(更保守,语义等价),但预研已确认会重跑,不应失败。Pydantic 往返若失败(节点拿到 dict),记录到 §Spike Conclusions,Task 3 节点改为"入口处对模型字段做 `Model(**x) if isinstance(x, dict) else x` 兜底"(退化),但预研已确认会重建,不应失败。

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_langgraph_orchestration_spike.py
git commit -m "test(spike): L2 langgraph 编排原语(interrupt/resume/rewind/config/往返/并发)"
```

---

### Task 2: PortoAgentState 加 `current_step` + reducer

**Files:**
- Modify: `backend/src/porto_chatbot/agent/state.py`
- Test: `backend/tests/test_agent_graph.py`(本 task 建文件,只放 reducer 单测;后续 task 追加 graph 拓扑测试)

**Interfaces:**
- Produces: `PortoAgentState` 含 `current_step: str`;`steps` 带 `operator.add` reducer;`specs`/`spec_results` 带 dict-merge reducer(`_dict_merge`)。模块级 `_dict_merge(left, right) -> dict`。

- [ ] **Step 1: 写失败测试(reducer 行为)**

`backend/tests/test_agent_graph.py`:

```python
"""agent graph:state reducer + 拓扑 + interrupt + update_state。"""
from __future__ import annotations

import operator
from typing import get_type_hints

from porto_chatbot.agent.state import PortoAgentState, _dict_merge


def test_dict_merge_reducer():
    assert _dict_merge(None, {"a": 1}) == {"a": 1}
    assert _dict_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert _dict_merge({"a": 1, "x": 9}, {"a": 2}) == {"a": 2, "x": 9}  # 右覆盖 + 保旧


def test_state_reducer_annotations():
    """steps→operator.add(append);specs/spec_results→_dict_merge;current_step 存在。"""
    th = get_type_hints(PortoAgentState, include_extras=True)
    assert operator.add in th["steps"].__metadata__
    assert _dict_merge in th["specs"].__metadata__
    assert _dict_merge in th["spec_results"].__metadata__
    assert "current_step" in th
```

- [ ] **Step 2: 运行,确认 FAIL**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_agent_graph.py -v`
Expected: FAIL(`_dict_merge` 不存在;`steps` 无 reducer metadata;无 `current_step`)。

- [ ] **Step 3: 改 state.py**

把 `backend/src/porto_chatbot/agent/state.py` 整体替换为:

```python
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from ..models import AgentStep, SourceChunk, SpecResult, Subsystem


def _dict_merge(left: dict, right: dict) -> dict:
    """dict-merge reducer:右覆盖左,保留左独有的 key。

    用于 specs / spec_results —— generate 节点写完整 dict,PATCH /specs 经
    graph.update_state 只改单个 key(merge),两者共用此 reducer。
    """
    return {**(left or {}), **(right or {})}


class PortoAgentState(TypedDict, total=False):
    workflow_id: str
    project_name: str
    prd_text: str
    sources: list[SourceChunk]
    understanding: str
    subsystems: list[Subsystem]
    specs: Annotated[dict[str, str], _dict_merge]
    spec_results: Annotated[dict[str, SpecResult], _dict_merge]
    evaluation: dict[str, Any]
    steps: Annotated[list[AgentStep], operator.add]
    top_k: int | None
    current_step: str
    rework_passes: int
    needs_rework: bool


DOMAIN_HINTS = {
    "user": ["用户", "账户", "认证", "登录", "权限", "profile", "account", "auth"],
    "order": ["订单", "下单", "履约", "交易", "order", "checkout"],
    "payment": ["支付", "收款", "退款", "结算", "payment", "refund", "settlement"],
    "notification": ["通知", "短信", "邮件", "站内信", "notification", "message"],
    "catalog": ["商品", "库存", "目录", "sku", "catalog", "inventory"],
    "risk": ["风控", "风险", "反欺诈", "审核", "risk", "fraud"],
    "reporting": ["报表", "统计", "分析", "dashboard", "report"],
}
```

> 说明(对设计 §6.2 的精炼,见 plan 顶部):`status` **不进** state —— 它由 executor 从 `get_state().next` 派生(`completed`/`awaiting_input`)或异常时写 `failed`,落入 `workflows.status` 表。`current_step` 由各节点写入(投影 + 状态同步读它)。

- [ ] **Step 4: 运行测试**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_agent_graph.py -v`
Expected: PASS。

- [ ] **Step 5: 确认未破坏既有套件**

Run: `cd backend && ./.venv/bin/python -m pytest -q`
Expected: 全绿(reducer 是惰性的,尚未被 graph 使用;既有节点仍按全量 state 返回,TypedDict 多了注解不影响)。

- [ ] **Step 6: 提交**

```bash
git add backend/src/porto_chatbot/agent/state.py backend/tests/test_agent_graph.py
git commit -m "refactor(agent): PortoAgentState 加 current_step + steps/specs reducer"
```

---

### Task 3: 节点签名 `(agent,state)→(state,*,config)` + graph 拓扑

> 本 task 后 `test_workflow_executor.py` / `test_workflow_api.py` 会 RED(见 Known middle states),Task 4 转绿。本 task 只看:`test_agent.py`(改调用方式)+ `test_agent_graph.py`(拓扑/interrupt)+ `test_langgraph_orchestration_spike.py` 须绿。

**Files:**
- Modify: `backend/src/porto_chatbot/agent/agent.py`(`_with_step`→`_step`)
- Modify: `backend/src/porto_chatbot/agent/nodes/{retrieve,understand,identify,generate,evaluate}.py`
- Create: `backend/src/porto_chatbot/agent/graph.py`
- Modify: `backend/tests/test_agent.py`(改 evaluate 调用)
- Modify: `backend/tests/test_agent_graph.py`(加拓扑/interrupt 测试)

**Interfaces:**
- Consumes: `PortoAgentState` reducers(Task 2)。
- Produces:
  - 节点签名统一为 `def fn(state: PortoAgentState, *, config: RunnableConfig) -> dict`(返回 **partial** 更新,含 `current_step` 与 `agent._step(...)` 产出的 `{"steps":[AgentStep]}`)。
  - `agent/graph.py`:`STEPS`、`INTERRUPT_AFTER` 常量;`build_workflow_graph(checkpointer) -> CompiledStateGraph`。

- [ ] **Step 1: 改 `PortoAgent._with_step` → `_step`(返回 partial)**

把 `backend/src/porto_chatbot/agent/agent.py` 的 `_with_step` 方法(约 66–78 行)替换为:

```python
    def _step(self, name: str, summary: str, data: dict[str, Any]) -> dict[str, Any]:
        """返回 partial 更新 ``{"steps": [AgentStep(...)]}`` + 记完成日志。

        节点把它 spread 进自己的返回值(steps 走 ``operator.add`` reducer 追加)。
        """
        self.logger.info("step completed name=%s summary=%s", name, summary)
        return {
            "steps": [AgentStep(name=name, status="completed", summary=summary, data=data)]
        }
```

同时把模块/类 docstring 里提及 `_with_step` 的字样改为 `_step`(约第 5、23 行的注释)。

- [ ] **Step 2: 写节点签名单测(先红)**

追加到 `backend/tests/test_agent_graph.py`:

```python
from unittest.mock import MagicMock

from porto_chatbot.agent.nodes import evaluate as evaluate_node
from porto_chatbot.agent.nodes import retrieve as retrieve_node
from porto_chatbot.agent.nodes import understand as understand_node


def _disabled_agent():  # llm.enabled=False → 走 fallback,不碰真 LLM/检索
    ag = MagicMock()
    ag.llm.enabled = False
    ag.logger.info = lambda *a, **k: None
    ag._step = lambda name, summary, data: {"steps": [{"name": name}]}
    return ag


def test_understand_node_reads_agent_from_config_and_returns_partial():
    state = {"workflow_id": "w", "project_name": "p", "prd_text": "需要一个订单管理模块", "steps": []}
    out = understand_node.understand_prd(state, config={"configurable": {"agent": _disabled_agent()}})
    # partial:不回传全量 state,只含改动键
    assert set(out).issubset({"understanding", "current_step", "steps"})
    assert out["current_step"] == "understand"
    assert out["understanding"]                      # fallback 产出非空


def test_retrieve_node_reads_agent_from_config():
    ag = _disabled_agent()
    ag.vector_store.ensure_index = lambda: None
    ag.vector_store.search = lambda q, top_k: []
    state = {"workflow_id": "w", "project_name": "p", "prd_text": "x", "top_k": 3, "steps": []}
    out = retrieve_node.retrieve_knowledge(state, config={"configurable": {"agent": ag}})
    assert out["current_step"] == "retrieve"
    assert out["sources"] == []
```

- [ ] **Step 3: 运行,确认 FAIL**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_agent_graph.py -v`
Expected: FAIL(节点仍是 `(agent, state)` 签名,`understand_prd(state, config=...)` 把 state 当 agent)。

- [ ] **Step 4: 迁移 5 个节点签名**

对每个节点文件,把 `def fn(agent, state)` 改为 `def fn(state, *, config)`,首行加 `agent = config["configurable"]["agent"]`,返回值从 `{**state, ...}` 全量改为 partial(含 `current_step`)。

`backend/src/porto_chatbot/agent/nodes/retrieve.py` —— 整体替换 `retrieve_knowledge`:

```python
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
```

`backend/src/porto_chatbot/agent/nodes/understand.py` —— 把 `def understand_prd(agent, state)` 改为:

```python
def understand_prd(state, *, config):
    agent = config["configurable"]["agent"]
    agent.logger.info("step understand_prd start workflow_id=%s", state.get("workflow_id"))
    understanding = ""
    if agent.llm.enabled:
        ctx = AgentToolContext(state=state, vector_store=agent.vector_store)
        result = agent.llm.complete_with_tools(
            "你是资深业务分析师。根据 PRD 和知识库片段，输出简洁的中文业务理解报告，"
            "包含：执行摘要、业务目标、核心实体、子系统线索。"
            "可调用工具获取 PRD 原文与检索知识库，自主决定检索什么。",
            "请生成业务理解报告。",
            build_agent_tools(ctx),
        )
        understanding = (result.text or "").strip()
        agent.logger.info(
            "step understand_prd llm tool_calls=%s turns=%s chars=%s",
            len(result.tool_calls),
            result.turns,
            len(understanding),
        )
    if not understanding:
        understanding = _fallback_understanding(state)
        agent.logger.info("step understand_prd used fallback workflow_id=%s", state.get("workflow_id"))
    return {
        "understanding": understanding,
        "current_step": "understand",
        **agent._step(
            "understand_prd",
            "完成业务理解报告",
            {"chars": len(understanding), "used_llm": bool(understanding) and agent.llm.enabled},
        ),
    }
```

`backend/src/porto_chatbot/agent/nodes/identify.py` —— 把 `def identify_subsystems(agent, state)` 改为 `def identify_subsystems(state, *, config)`,首行 `agent = config["configurable"]["agent"]`,把所有 `state[...]` 读取保留(入参 `state` 不变),把结尾 return 替换为:

```python
    return {
        "subsystems": subsystems,
        "current_step": "identify",
        **agent._step(
            "identify_subsystems",
            f"识别 {len(subsystems)} 个子系统",
            {
                "subsystems": [s.model_dump() for s in subsystems],
                "used_llm": bool(subsystems) and agent.llm.enabled,
            },
        ),
    }
```

`backend/src/porto_chatbot/agent/nodes/generate.py` —— 把 `def generate_specs(agent, state)` 改为 `def generate_specs(state, *, config)`,首行 `agent = config["configurable"]["agent"]`,函数体(`subs = state["subsystems"]`、`_gen`、ThreadPoolExecutor 分支)原样保留,结尾 return 替换为:

```python
    return {
        "specs": specs,
        "spec_results": results,
        "current_step": "generate",
        **agent._step(
            "generate_specs",
            f"生成 {len(specs)} 份子系统规格",
            {
                "spec_names": list(specs),
                "used_llm": used_llm,
                "iterations": total_iters,
                "attempts": {name: r.model_dump() for name, r in results.items()},
            },
        ),
    }
```

`backend/src/porto_chatbot/agent/nodes/evaluate.py` —— 把 `def evaluate(agent, state)` 改为 `def evaluate(state, *, config)`,首行 `agent = config["configurable"]["agent"]`,函数体原样保留(读 `state[...]`、`agent.settings`),结尾 return 替换为:

```python
    return {
        "evaluation": evaluation,
        "rework_passes": passes + 1 if needs_rework else passes,
        "needs_rework": needs_rework,
        "current_step": "evaluate",
        **agent._step("evaluate", f"评估得分 {evaluation['score']}", evaluation),
    }
```

- [ ] **Step 5: 创建 `agent/graph.py`**

`backend/src/porto_chatbot/agent/graph.py`:

```python
"""Porto workflow graph:线性 retrieve→understand→identify→generate→evaluate,
interrupt_after 一比一替换旧 CHECKPOINTS。

节点签名 ``(state, *, config) -> partial``,agent 经 ``config["configurable"]["agent"]`` 注入。
STEPS / INTERRUPT_AFTER 从已删除的 workflow_runner 迁入此处(拓扑定义的归属地)。
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
    for a, b in zip(STEPS, STEPS[1:]):
        g.add_edge(a, b)
    g.add_edge(STEPS[-1], END)
    return g.compile(checkpointer=checkpointer, interrupt_after=INTERRUPT_AFTER)
```

- [ ] **Step 6: 写 graph 拓扑 / interrupt 测试(用 stand-in 节点,隔离真节点逻辑)**

追加到 `backend/tests/test_agent_graph.py`:

```python
import sqlite3

from porto_chatbot.agent.graph import STEPS, INTERRUPT_AFTER, build_workflow_graph


def _tmp_saver(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "g.sqlite3"), check_same_thread=False)
    from langgraph.checkpoint.sqlite import SqliteSaver
    sv = SqliteSaver(conn)
    sv.setup()
    return sv


def test_constants_match_design():
    assert STEPS == ["retrieve", "understand", "identify", "generate", "evaluate"]
    assert INTERRUPT_AFTER == ["understand", "identify", "generate"]


def test_graph_interrupt_points_with_standin_nodes(tmp_path, monkeypatch):
    """用 stand-in 节点替换真节点,验证拓扑 + interrupt_after 位置(不依赖 LLM/检索)。"""
    import porto_chatbot.agent.graph as graph_mod

    order: list[str] = []

    def mk(name):
        def fn(state, *, config):
            order.append(name)
            return {"current_step": name}
        return fn

    # 临时把 _NODE_FNS 指向 stand-in(只测拓扑,不跑真节点)
    saved = dict(graph_mod._NODE_FNS)
    for n in STEPS:
        graph_mod._NODE_FNS[n] = mk(n)
    try:
        graph = build_workflow_graph(_tmp_saver(tmp_path))
    finally:
        graph_mod._NODE_FNS.clear()
        graph_mod._NODE_FNS.update(saved)

    cfg = {"configurable": {"thread_id": "w1", "agent": None}}
    graph.invoke({}, cfg)
    assert order == ["retrieve", "understand"]          # 停在 understand 后
    assert list(graph.get_state(cfg).next) == ["identify"]

    order.clear()
    list(graph.stream(None, cfg))
    assert order == ["identify"]                         # 推一格到 identify
    assert list(graph.get_state(cfg).next) == ["generate"]

    order.clear()
    list(graph.stream(None, cfg))                        # generate 后停
    assert order == ["generate"]
    order.clear()
    list(graph.stream(None, cfg))                        # evaluate → END
    assert order == ["evaluate"]
    assert list(graph.get_state(cfg).next) == []
```

- [ ] **Step 7: 改 `test_agent.py` 的 evaluate 调用**

`backend/tests/test_agent.py` 里所有 `evaluate_node.evaluate(agent, state)` 改为 `evaluate_node.evaluate(state, config={"configurable": {"agent": agent}})`。涉及 `test_evaluate_aggregates_spec_rubric_scores`、`test_evaluate_marks_rework_on_low_rubric`、`test_evaluate_no_rework_on_high_rubric` 等全部 evaluate 直调处(grep `evaluate_node.evaluate(` 定位)。每个调用前已有 `agent = PortoAgent(sample_settings)` 与 `state = _eval_state(...)`,改为:

```python
    result = evaluate_node.evaluate(state, config={"configurable": {"agent": agent}})
```

断言不变(`result["evaluation"][...]` / `result["needs_rework"]` 等读 partial 返回的键,语义一致)。

- [ ] **Step 8: 运行本 task 测试**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_agent_graph.py tests/test_agent.py tests/test_langgraph_orchestration_spike.py tests/test_spec_loop.py -v`
Expected: 全 PASS。

- [ ] **Step 9: 确认中间态红窗(预期)**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_workflow_executor.py tests/test_workflow_api.py -q 2>&1 | tail -3`
Expected: **RED**(executor 仍调旧 runner,节点签名已变 → 崩)。这是 Known middle state,Task 4 修。

- [ ] **Step 10: 提交**

```bash
git add backend/src/porto_chatbot/agent/agent.py backend/src/porto_chatbot/agent/nodes backend/src/porto_chatbot/agent/graph.py backend/tests/test_agent_graph.py backend/tests/test_agent.py
git commit -m "refactor(agent): 节点签名 agent→config + build_workflow_graph"
```

---

### Task 4: Executor 切到 graph(invoke/stream/投影)+ deps + 删 WorkflowRunner

> 本 task 把 executor 从 `WorkflowRunner` 切到 `StateGraph`,并删除 `workflow_runner.py` + 其测试。落地后 `test_workflow_executor.py`(重写)与 `test_workflow_api.py` 的 create/advance/list/delete/concurrent-409/cross-checkpoint 用例转绿。PUT/PATCH 仍走 store(Task 5 改),其 API 测试此时也绿(store 路径未动)。

**Files:**
- Modify: `backend/src/porto_chatbot/workflow_executor.py`(重写内部)
- Delete: `backend/src/porto_chatbot/workflow_runner.py`
- Delete: `backend/tests/test_workflow_runner.py`
- Modify: `backend/src/porto_chatbot/api/deps.py`(加 checkpointer/graph 单例;executor 传 graph;reset 关 conn)
- Modify: `backend/tests/test_workflow_executor.py`(重写:注入测试 graph)

**Interfaces:**
- Consumes: `build_workflow_graph`、`STEPS`(Task 3);`WorkflowStore`(不变)。
- Produces:
  - `WorkflowExecutor(settings, store, graph)` —— `__init__` 新增 `graph` 参数。
  - `start_workflow(wid)` / `advance(wid) -> bool`:后台线程跑 `graph.invoke(initial, config)` / `list(graph.stream(None, config))`,完成后投影 + 状态同步。
  - `_project_state(wid, config, produced_by_overrides=None, default_produced_by="ai")`:从 `graph.get_state(config)` 投影已完成步到 `workflow_outputs`(保既有 produced_by,清下游)。
  - `_sync_status(wid, config)`:`next` 空→`completed`,否则→`awaiting_input`;`current_step` 取 state。

- [ ] **Step 1: 重写 `workflow_executor.py`**

整体替换 `backend/src/porto_chatbot/workflow_executor.py` 为:

```python
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
import threading
import time
from typing import Any

from pydantic import BaseModel

from .agent import PortoAgent
from .agent.graph import STEPS
from .agent.heuristics import infer_project_name
from .llm import LLMClient
from .logging_utils import get_component_logger
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
                self.store.update_status(workflow_id, "failed", error="worker crashed")
            except Exception:
                pass
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
        self.store.update_status(workflow_id, "running")
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
        except Exception as exc:
            logger.exception("workflow start failed workflow_id=%s", workflow_id)
            self.store.update_status(workflow_id, "failed", error=str(exc))
            return
        self._project_state(workflow_id, config)
        self._sync_status(workflow_id, config)

    def _run_advance(self, workflow_id: str) -> None:
        row = self.store.get(workflow_id)
        if row is None:
            logger.warning("workflow not found workflow_id=%s", workflow_id)
            return
        self.store.update_status(workflow_id, "running")
        agent = self._build_agent(row)
        config = self._config(workflow_id, agent)
        try:
            list(self.graph.stream(None, config))  # 续跑到下个 interrupt / END
        except Exception as exc:
            logger.exception("workflow advance failed workflow_id=%s", workflow_id)
            self.store.update_status(workflow_id, "failed", error=str(exc))
            return
        self._project_state(workflow_id, config)
        self._sync_status(workflow_id, config)

    def _completed_steps(self, config: dict) -> list[str]:
        """按 ``get_state().next`` 算已完成步(投影到 next 之前,含)。"""
        snap = self.graph.get_state(config)
        nxt = list(snap.next or [])
        if not nxt:
            return list(STEPS)                       # 到 END,全部完成
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
                k: _to_jsonable(values[k])
                for k in _STEP_OUTPUT_KEYS.get(step, [])
                if k in values
            }
            if not out:
                continue
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

    def _sync_status(self, workflow_id: str, config: dict) -> None:
        snap = self.graph.get_state(config)
        status = "completed" if not snap.next else "awaiting_input"
        self.store.update_status(
            workflow_id, status, current_step=snap.values.get("current_step")
        )
```

- [ ] **Step 2: 删除 workflow_runner + 其测试**

```bash
rm backend/src/porto_chatbot/workflow_runner.py
rm backend/tests/test_workflow_runner.py
```

(executor 已不 import 它;`STEPS` 现来自 `agent.graph`。)

- [ ] **Step 3: deps 加 checkpointer / graph 单例 + executor 传 graph + reset 关 conn**

在 `backend/src/porto_chatbot/api/deps.py`:

3a. `get_workflow_executor`(约 216–229 行)改为:

```python
def get_workflow_executor() -> WorkflowExecutor:
    """按 data_dir 缓存的 WorkflowExecutor 单例(懒加载),注入共享 graph。"""
    from ..workflow_executor import WorkflowExecutor

    entry = _ensure_rag_singletons()
    ex = entry.get("workflow_executor")
    if ex is None:
        ex = WorkflowExecutor(current_settings(), get_workflow_store(), get_workflow_graph())
        entry["workflow_executor"] = ex
    return ex
```

3b. 在 `get_workflow_store`(约 200–213 行)之后、`get_workflow_executor` 之前,新增:

```python
def get_checkpointer():
    """按 data_dir 缓存的 langgraph SqliteSaver 单例(独立于 workflows.sqlite3)。

    U3(L1 spike)已确认 SqliteSaver 自带 threading.Lock 序列化 SQLite 访问,
    多 daemon 线程共享同一 connection(check_same_thread=False)安全。
    """
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    entry = _ensure_rag_singletons()
    cp = entry.get("checkpointer")
    if cp is None:
        settings = current_settings()
        db_path = settings.data_dir / "langgraph_checkpoints.sqlite"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        cp = SqliteSaver(conn)
        cp.setup()
        entry["checkpointer"] = cp
        entry["_checkpoint_conn"] = conn
    return cp


def get_workflow_graph():
    """按 data_dir 缓存的编译后 workflow graph 单例(挂共享 checkpointer)。"""
    from ..agent.graph import build_workflow_graph

    entry = _ensure_rag_singletons()
    g = entry.get("workflow_graph")
    if g is None:
        g = build_workflow_graph(get_checkpointer())
        entry["workflow_graph"] = g
    return g
```

3c. `reset_rag_singletons`(约 232–244 行)末尾追加 best-effort 关 conn(在循环体内):

```python
        try:
            entry["_checkpoint_conn"].close()
        except Exception:
            pass
```

- [ ] **Step 4: 重写 `test_workflow_executor.py`(注入测试 graph)**

整体替换 `backend/tests/test_workflow_executor.py` 为:

```python
"""WorkflowExecutor —— 后台线程用 langgraph graph 推进 + per-workflow 锁防并发 advance。

测试用 trivial graph(stand-in 节点 + 临时 SqliteSaver)注入 executor,验证:
start/advance/failed/并发 guard/投影保 produced_by。不跑真节点逻辑。
"""
from __future__ import annotations

import sqlite3
import threading

from langgraph.graph import START, END, StateGraph

from porto_chatbot.agent.graph import STEPS
from porto_chatbot.settings import Settings
from porto_chatbot.workflow_executor import WorkflowExecutor
from porto_chatbot.workflow_store import WorkflowStore


def _saver(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "ex.sqlite3"), check_same_thread=False)
    from langgraph.checkpoint.sqlite import SqliteSaver
    sv = SqliteSaver(conn)
    sv.setup()
    return sv


#: stand-in 节点每个 step 写的产出(注意 specs/spec_results 必须是 dict ——
#: PortoAgentState 上它们带 dict-merge reducer,传字符串会让 reducer 崩)。
_OUT_VALS = {
    "retrieve": {"sources": "retrieve-val"},
    "understand": {"understanding": "understand-val"},
    "identify": {"subsystems": "identify-val"},
    "generate": {"specs": {"default": "generate-val"}, "spec_results": {"default": "gen"}},
    "evaluate": {"evaluation": "evaluate-val"},
}


def _trivial_graph(tmp_path, *, fail_on=None, slow_step=None, slow_event=None):
    """与真图同拓扑 + interrupt_after 的 stand-in 图;每节点写产出键 + current_step。

    fail_on: 命中该 step 时抛异常(测 failed 路径)。
    slow_step + slow_event: 仅当运行到 slow_step 时 set(enter) 并阻塞于 release.wait
        (测 guard:让 worker 进入该节点后持续持 guard;其余节点不阻塞)。
    """
    from porto_chatbot.agent.state import PortoAgentState

    def mk(name):
        def fn(state, *, config):
            if slow_event and name == slow_step:
                enter, release = slow_event
                enter.set()
                release.wait(timeout=5.0)
            if fail_on and name == fail_on:
                raise RuntimeError("boom")
            return {**_OUT_VALS[name], "current_step": name}
        return fn

    g = StateGraph(PortoAgentState)
    for n in STEPS:
        g.add_node(n, mk(n))
    g.add_edge(START, STEPS[0])
    for a, b in zip(STEPS, STEPS[1:]):
        g.add_edge(a, b)
    g.add_edge(STEPS[-1], END)
    return g.compile(checkpointer=_saver(tmp_path),
                     interrupt_after=["understand", "identify", "generate"])


def _make(tmp_path, **kw):
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    ex = WorkflowExecutor(settings, store, _trivial_graph(tmp_path, **kw))
    return ex, store


def _create(store):
    return store.create("s", "p", "prd", 6, {"embedding_provider": "local"},
                        {"agent_provider": "openai"})


def test_start_runs_to_understand_checkpoint(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    row = store.get(wid)
    assert row["status"] == "awaiting_input"
    assert row["current_step"] == "understand"
    outs = store.get_outputs(wid)
    assert outs["retrieve"]["output"]["sources"] == "retrieve-val"
    assert outs["understand"]["output"]["understanding"] == "understand-val"


def test_advance_runs_next_checkpoint(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid); ex.wait(wid, timeout=5)
    assert ex.advance(wid) is True
    ex.wait(wid, timeout=5)
    assert store.get(wid)["current_step"] == "identify"


def test_advance_to_completed(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid); ex.wait(wid, timeout=5)
    for _ in range(3):  # understand→identify→generate→evaluate(END)
        assert ex.advance(wid) is True
        ex.wait(wid, timeout=5)
    assert store.get(wid)["status"] == "completed"
    assert store.get(wid)["current_step"] == "evaluate"


def test_advance_returns_false_when_running(tmp_path):
    """advance 时 worker 进入 slow 节点(identify)持续持 guard:再 advance 必 False。"""
    enter, release = threading.Event(), threading.Event()
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    ex = WorkflowExecutor(
        settings, store,
        _trivial_graph(tmp_path, slow_step="identify", slow_event=(enter, release)),
    )
    wid = _create(store)
    ex.start_workflow(wid); ex.wait(wid, timeout=5)   # 停 understand;identify 未触发,不阻塞
    assert ex.advance(wid) is True                     # worker 进 identify → 阻塞、持 guard
    assert enter.wait(timeout=2.0)
    assert ex.advance(wid) is False                    # guard 被持 → 409 语义
    release.set()
    ex.wait(wid, timeout=5)


def test_failed_records_error(tmp_path):
    ex, store = _make(tmp_path, fail_on="understand")
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    row = store.get(wid)
    assert row["status"] == "failed"
    assert "boom" in row["error"]


def test_persist_preserves_user_produced_by(tmp_path):
    """advance 后,更早的(用户编辑过)步 produced_by 保持 user;新步为 ai。

    模拟 understand 已是 produced_by=user(store 直接造),advance 跑 identify。
    投影:understand 内容来自 graph state(stand-in 的 "understand-val",与 store 的
    "user-edited" 不同 → 视为变化 → 重写,但 produced_by 取既有 "user" 保留);identify 新步为 ai。
    """
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid); ex.wait(wid, timeout=5)          # 停 understand
    store.save_output(wid, "understand", {"understanding": "user-edited"}, "user")

    assert ex.advance(wid) is True
    ex.wait(wid, timeout=5)                                   # → identify

    outs = store.get_outputs(wid)
    assert outs["understand"]["produced_by"] == "user"        # 未被覆盖为 ai
    assert outs["identify"]["produced_by"] == "ai"


def test_two_rapid_advances_first_wins(tmp_path):
    """回归:两次 rapid advance 同一 workflow,第一次 True(advance 自己拿 guard 起worker),
    第二次 False(guard 被 worker 持有)。单图、slow_step=identify,不换图不换 saver。"""
    enter, release = threading.Event(), threading.Event()
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    ex = WorkflowExecutor(
        settings, store,
        _trivial_graph(tmp_path, slow_step="identify", slow_event=(enter, release)),
    )
    wid = _create(store)
    ex.start_workflow(wid); ex.wait(wid, timeout=5)          # 停 understand,guard 空闲

    r1 = ex.advance(wid)
    assert r1 is True                                         # advance 自己拿 guard 起 worker
    assert enter.wait(timeout=2.0)                            # worker 已进 identify、持 guard
    r2 = ex.advance(wid)
    assert r2 is False                                        # guard 被持
    release.set()
    ex.wait(wid, timeout=5)
    assert store.get(wid)["current_step"] == "identify"
```

- [ ] **Step 5: 运行 executor 测试**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_workflow_executor.py -v`
Expected: 全 PASS。

- [ ] **Step 6: 运行 API 测试(create/advance/list/delete 转绿)**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_workflow_api.py -v`
Expected: `test_workflow_checkpoint_flow`、`test_list_and_delete`、`test_advance_concurrent_returns_409`、`test_advance_past_first_checkpoint_reaches_identify` PASS(PUT/PATCH 用例此时走 store,也 PASS)。chromadb 偶发 flake(L1 已知,非本 plan 引入)可重试。

- [ ] **Step 7: 运行 store / startup 测试(须仍绿)**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_workflow_store.py tests/test_workflow_startup_recovery.py -v`
Expected: PASS(startup 用 store 直接造 running、无 checkpoint;Task 5 才改 lifespan)。

- [ ] **Step 8: 提交**

```bash
git add backend/src/porto_chatbot/workflow_executor.py backend/src/porto_chatbot/api/deps.py backend/tests/test_workflow_executor.py
git rm backend/src/porto_chatbot/workflow_runner.py backend/tests/test_workflow_runner.py
git commit -m "refactor(workflow): executor 切 langgraph graph + 删 WorkflowRunner"
```

---

### Task 5: Executor `update_step`/`update_spec`/`recover` + 路由 + lifespan

**Files:**
- Modify: `backend/src/porto_chatbot/workflow_executor.py`(加 3 方法)
- Modify: `backend/src/porto_chatbot/api/routes/workflow.py`(PUT/PATCH 改调 executor)
- Modify: `backend/src/porto_chatbot/api/app.py`(lifespan 调 recover_on_startup)
- Modify: `backend/src/porto_chatbot/workflow_store.py`(删 `mark_running_interrupted_on_startup`)
- Modify: `backend/tests/test_workflow_store.py`(删对应 2 测试)
- Modify: `backend/tests/test_workflow_startup_recovery.py`(保持绿 + 加 at-interrupt→awaiting 用例)

**Interfaces:**
- Produces:
  - `WorkflowExecutor.update_step(wid, step, body)`:`graph.update_state(config, {**body, "current_step":step}, as_node=step)` + 投影(step 强制 `produced_by="user"`)+ 状态同步。
  - `WorkflowExecutor.update_spec(wid, name, body) -> bool`:`store.update_spec`(审计,保留 produced_by)+ best-effort `graph.update_state(config, {"specs":{name:body}})`(无 checkpoint 则跳过)。
  - `WorkflowExecutor.recover_on_startup()`:扫 `status="running"`,按 `get_state().next` 判定(at interrupt→awaiting_input;无 checkpoint→interrupted 保 current_step)。

- [ ] **Step 1: 给 executor 加 3 方法**

在 `backend/src/porto_chatbot/workflow_executor.py` 的 `_sync_status` 之后追加:

```python
    # ----------------------------------------------------- PUT / PATCH / recovery

    def update_step(self, workflow_id: str, step: str, body: dict[str, Any]) -> None:
        """PUT /steps/{step}:覆盖产出 + 回退到该步(用户编辑)。

        graph.update_state(as_node=step) 把图位置回退到该步之后(下游重算);
        投影把该步标记 produced_by="user",清下游(等价旧 clear_outputs_after)。
        update_state 不执行节点,不需要 agent。
        """
        config = self._config(workflow_id)
        self.graph.update_state(
            config, {**body, "current_step": step}, as_node=step
        )
        self._project_state(
            workflow_id, config, produced_by_overrides={step: "user"}
        )
        self._sync_status(workflow_id, config)

    def update_spec(self, workflow_id: str, name: str, body: str) -> bool:
        """PATCH /specs:轻量改单个 spec(审计 + graph state)。

        store.update_spec 改 workflow_outputs(specs dict-merge,不动 produced_by/下游/status);
        再 best-effort 同步到 graph state(无 checkpoint —— 如手工造的合成 workflow —— 则跳过)。
        """
        if not self.store.update_spec(workflow_id, name, body):
            return False
        config = self._config(workflow_id)
        try:
            self.graph.update_state(config, {"specs": {name: body}})
        except Exception:
            # 无 checkpoint 或 graph 不可写:审计已落 store,graph 同步跳过
            logger.warning("update_spec graph sync skipped workflow_id=%s", workflow_id, exc_info=True)
        return True

    def recover_on_startup(self) -> int:
        """启动恢复:扫 status="running"。

        - 有 checkpoint 且在 interrupt 处(next 非空)→ awaiting_input(可继续 advance);
        - 有 checkpoint 但 next 空(异常态)→ interrupted;
        - 无 checkpoint(如崩溃前未真正跑过 / 合成数据)→ interrupted,保留既有 current_step。
        返回处理条数。
        """
        rows, _ = self.store.list_workflows(status="running")
        for r in rows:
            wid = r["workflow_id"]
            config = self._config(wid)
            try:
                snap = self.graph.get_state(config)
            except Exception:
                logger.warning("recover get_state failed workflow_id=%s", wid, exc_info=True)
                snap = None
            if snap and snap.next:
                self.store.update_status(
                    wid, "awaiting_input", current_step=snap.values.get("current_step")
                )
            elif snap and snap.values:
                self.store.update_status(
                    wid, "interrupted", current_step=snap.values.get("current_step")
                )
            else:
                self.store.update_status(wid, "interrupted")  # current_step=None 保既有
        return len(rows)
```

- [ ] **Step 2: 写 update_step / update_spec / recover 测试**

追加到 `backend/tests/test_workflow_executor.py`(复用 Task 4 的 `_make`/`_create`/`_trivial_graph`):

```python
def test_update_step_rewinds_and_marks_user(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid); ex.wait(wid, timeout=5)        # understand
    ex.advance(wid); ex.wait(wid, timeout=5)               # identify
    assert "identify" in store.get_outputs(wid)

    ex.update_step(wid, "understand", {"understanding": "edited"})
    row = store.get(wid)
    assert row["current_step"] == "understand"
    assert row["status"] == "awaiting_input"
    outs = store.get_outputs(wid)
    assert outs["understand"]["produced_by"] == "user"
    assert outs["understand"]["output"]["understanding"] == "edited"
    assert "identify" not in outs                           # 下游被清


def test_update_spec_updates_store_and_graph(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    # 跑到 generate checkpoint(trivial 图 generate 写 specs={"default": ...})
    ex.start_workflow(wid); ex.wait(wid, timeout=5)
    for _ in range(2):  # → identify → generate
        ex.advance(wid); ex.wait(wid, timeout=5)
    assert store.get(wid)["current_step"] == "generate"

    ok = ex.update_spec(wid, "default", "new body")          # 改 specs 里的 "default" key
    assert ok is True
    outs = store.get_outputs(wid)
    assert outs["generate"]["produced_by"] == "ai"           # 审计不动
    assert outs["generate"]["output"]["specs"]["default"] == "new body"
    # graph state 已 dict-merge(specs 是 dict-merge reducer)
    cfg = {"configurable": {"thread_id": wid}}
    assert ex.graph.get_state(cfg).values["specs"]["default"] == "new body"


def test_update_spec_missing_returns_false(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    assert ex.update_spec(wid, "nope", "x") is False        # 无 generate output


def test_recover_at_interrupt_marks_awaiting(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid); ex.wait(wid, timeout=5)         # understand(checkpoint 在)
    store.update_status(wid, "running")                     # 模拟崩溃时 status=running
    n = ex.recover_on_startup()
    assert n == 1
    assert store.get(wid)["status"] == "awaiting_input"     # checkpoint 在 interrupt → 可续


def test_recover_no_checkpoint_marks_interrupted(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    store.update_status(wid, "running", current_step="understand")  # 从未跑过 graph
    n = ex.recover_on_startup()
    assert n == 1
    row = store.get(wid)
    assert row["status"] == "interrupted"
    assert row["current_step"] == "understand"              # 无 checkpoint → 保既有
```

- [ ] **Step 3: 运行**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_workflow_executor.py -v`
Expected: 全 PASS(含新 5 用例)。

- [ ] **Step 4: 路由 PUT/PATCH 改调 executor**

在 `backend/src/porto_chatbot/api/routes/workflow.py`:

把 `save_step_output`(PUT,约 289–309 行)的 store 三行替换为 executor 调用:

```python
@router.put("/api/porto/workflows/{workflow_id}/steps/{step}", response_model=WorkflowDetail)
def save_step_output(workflow_id: str, step: str, body: dict[str, Any]):
    """覆盖某步产出并回退到该步(用户编辑)。

    executor.update_step:graph.update_state(as_node=step) 回退图位置 + 投影(edited→user)
    + 清下游 + status/current_step 同步。step 必须在 {understand, identify, generate}。
    """
    store = get_workflow_store()
    if store.get(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    if step not in _EDITABLE_STEPS:
        raise HTTPException(400, "step is not editable")
    get_workflow_executor().update_step(workflow_id, step, body)
    return _detail(store, workflow_id)
```

把 `update_spec`(PATCH,约 312–322 行)改为:

```python
@router.patch("/api/porto/workflows/{workflow_id}/specs", response_model=WorkflowDetail)
def update_spec(workflow_id: str, payload: SpecUpdateRequest):
    """轻量更新某个 spec 正文:executor.update_spec(审计 + graph state dict-merge),
    不动 status/current_step、不清下游、不改 produced_by。workflow 不存在→404;
    无 generate output 或 name 不在 specs→400。"""
    store = get_workflow_store()
    if store.get(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    if not get_workflow_executor().update_spec(workflow_id, payload.name, payload.body):
        raise HTTPException(400, "spec not found")
    return _detail(store, workflow_id)
```

- [ ] **Step 5: lifespan 调 recover_on_startup**

读 `backend/src/porto_chatbot/api/app.py`(约 30–43 行 lifespan)。把 `n = get_workflow_store().mark_running_interrupted_on_startup()` 改为:

```python
    n = get_workflow_executor().recover_on_startup()
```

(并在该行日志里把 "running→interrupted" 措辞改为 "running workflows recovered" 之类,`n` 仍是条数。)需在 app.py 顶部 import `get_workflow_executor`(若未导入,加到现有 deps import)。

- [ ] **Step 6: 删 store.mark_running_interrupted_on_startup + 其测试**

在 `backend/src/porto_chatbot/workflow_store.py` 删除 `mark_running_interrupted_on_startup` 方法(约 192–198 行)。

在 `backend/tests/test_workflow_store.py` 删除 `test_mark_running_interrupted_on_startup`(约 78–87 行)。

- [ ] **Step 7: startup 恢复 API 测试**

`backend/tests/test_workflow_startup_recovery.py` 现有用例(`test_startup_marks_running_interrupted`)保持不变:它用 store 直接造 `running`、无 graph checkpoint → `recover_on_startup` 走"无 checkpoint→interrupted、保 current_step"分支。确认仍 PASS(这同时验证了 lifespan→`recover_on_startup` 的接线)。

> "checkpoint 在 interrupt→awaiting_input"分支由 Task 5 Step 2 的 `test_recover_at_interrupt_marks_awaiting`(trivial 图,有 checkpoint)单元覆盖;无需在 API 层另起一个会跑真节点的用例(避免 startup 测试引入索引/检索副作用)。

- [ ] **Step 8: 运行全量相关测试**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_workflow_executor.py tests/test_workflow_store.py tests/test_workflow_startup_recovery.py tests/test_workflow_api.py -v`
Expected: 全 PASS。`test_save_step_output_overwrites_and_rewinds`(PUT)断言 `produced_by=="user"` + `status=="awaiting_input"` 仍成立;`test_update_spec`(PATCH,合成数据无 checkpoint)走 best-effort graph 同步(跳过)、store 已改 → 断言成立。

- [ ] **Step 9: 提交**

```bash
git add backend/src/porto_chatbot/workflow_executor.py backend/src/porto_chatbot/api/routes/workflow.py backend/src/porto_chatbot/api/app.py backend/src/porto_chatbot/workflow_store.py backend/tests/test_workflow_executor.py backend/tests/test_workflow_store.py backend/tests/test_workflow_startup_recovery.py
git commit -m "refactor(workflow): PUT/PATCH 走 graph update_state + 启动恢复查 checkpoint"
```

---

### Task 6: 全量回归 + ruff + 降级冒烟 + 收尾

**Files:**
- 无源码改动(仅校验 + 文档/memory)。

- [ ] **Step 1: 全量测试**

Run: `cd backend && ./.venv/bin/python -m pytest -q`
Expected: 全绿(L1 的 176 + L2 新增,0 fail)。chromadb 偶发 flake(L1 已知,pre-existing)可重试一次确认非本 plan 引入。

- [ ] **Step 2: ruff**

Run: `cd backend && ./.venv/bin/python -m ruff check src tests`
Expected: clean(本 plan 引入的代码 0 告警;pre-existing 的 11 项不阻塞,与 main 一致)。

- [ ] **Step 3: 降级路径冒烟(无 key,真 graph)**

Run:
```bash
cd backend && rm -rf /tmp/porto_l2_smoke && mkdir -p /tmp/porto_l2_smoke/kb && PORTO_CHATBOT_DATA_DIR=/tmp/porto_l2_smoke ./.venv/bin/python -c "
from porto_chatbot.settings import Settings
from porto_chatbot.workflow_store import WorkflowStore
from porto_chatbot.workflow_executor import WorkflowExecutor
from porto_chatbot.agent.graph import build_workflow_graph
import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver
s = Settings(kb_dirs=['/tmp/porto_l2_smoke/kb'], data_dir='/tmp/porto_l2_smoke', log_dir='/tmp/porto_l2_smoke')
store = WorkflowStore(s)
conn = sqlite3.connect('/tmp/porto_l2_smoke/cp.sqlite3', check_same_thread=False); cp = SqliteSaver(conn); cp.setup()
ex = WorkflowExecutor(s, store, build_workflow_graph(cp))
wid = store.create('s','p','需要一个订单管理模块,支持下单和支付',6,{'embedding_provider':'local'},{'agent_provider':'openai'})
ex.start_workflow(wid); ex.wait(wid, timeout=30)
row = store.get(wid)
assert row['status'] in ('awaiting_input','completed'), row
assert row['current_step'] in ('understand','evaluate'), row
print('degradation ok:', row['status'], row['current_step'])
"
```
Expected: 输出 `degradation ok: ...`(graph 在无 LLM 下走 fallback 推进到 understand/evaluate)。

- [ ] **Step 4: API 契约不变性核查**

Run: `cd backend && git diff main -- src/porto_chatbot/api/routes/workflow.py | grep -E '^\+.*@router|^\+.*response_model|^\+.*HTTPException' | head`
Expected: 无新增/删除 endpoint 路径或 response_model(仅 PUT/PATCH 内部实现改调 executor);`WorkflowCreated`/`WorkflowDetail`/`WorkflowListItem`/`SpecUpdateRequest` 模型未改。

- [ ] **Step 5: 更新设计文档 §11(spike 结论回填)**

把 §Spike Conclusions 的结论同步到 `docs/superpowers/specs/2026-07-24-langchain-langgraph-migration-design.md` §11(把 U3 的 L2 集成确认补一行;如适用,加 Pydantic 往返 + deprecation warning 的 followup)。

- [ ] **Step 6: 更新 memory(chatbot-agent-modernization)**

更新 `/Users/yuhaochen/.claude/projects/-Users-yuhaochen-Documents-codebase-projanvil-Porto/memory/chatbot-agent-modernization.md`:把"agent 架构现状"从"手写 WorkflowRunner 状态机"更新为"langgraph StateGraph 编排层(L2 已合入)";记 L3 待办(spec 子图 + Send map-reduce,U1/U2 spike 已铺路)。

- [ ] **Step 7: 提交 + finishing**

```bash
git add docs/superpowers/specs/2026-07-24-langchain-langgraph-migration-design.md
git commit -m "docs(spec): L2 spike 结论回填 + langgraph 编排层落地"
```

随后用 `superpowers:finishing-a-development-branch` 决定合并/PR。

---

## Spike Conclusions

> Task 1 跑完后把结论填这里(预研已验证,implementer 确认)。

- **① interrupt_after + stream(None) 续跑**: ✅ langgraph 1.2.9 下 `interrupt_after=["understand"]` 使 `invoke` 在该节点之后暂停(`get_state().next=["identify"]`),`list(graph.stream(None, config))` 续跑到下个 interrupt / END。(预研已验证)
- **② update_state(as_node=) 回退 + 下游重算**: ✅ `update_state(config, {...}, as_node="a")` 把 `next` 重置为 a 的后继(`["b"]`),续跑时 **重跑 b**(下游重算)。(预研已验证)
- **③ configurable 注入 agent**: ✅ 节点签名 `(state, *, config)`,`config["configurable"]["agent"]` 取到注入对象。(预研已验证)
- **④ Pydantic 模型过 SqliteSaver 往返**: ✅ `SourceChunk`/`Subsystem`/`SpecResult`/`SpecAttempt` 等 `BaseModel` 经 checkpoint 序列化/反序列化后**仍是模型实例**(属性访问可用),无需 `_rebuild_state` 式 dict→model 重建。**注意**:langgraph 会发 deprecation warning("Deserializing unregistered type ... add to allowed_msgpack_modules")—— 当前(1.2.9)仅警告不阻断;**followup**:若未来 langgraph 升级阻断,需注册 `porto_chatbot.models` 到 checkpointer 的 serde 或 pin 版本(非 L2 阻塞项)。
- **⑤ 共享 SqliteSaver 多 workflow 并发**: ✅ 单 `SqliteSaver`(共享 connection,`check_same_thread=False`)在 8 个 daemon 线程(不同 thread_id)下无冲突(与 L1 U3 单元结论一致,集成层再确认)。L2 直接用 `SqliteSaver`,不需 Async/串行化层。
- **对设计 §6.2 的精炼**:`status` 不进 graph state(executor 从 `get_state().next` 派生),仅 `current_step` 进 state。

## L2 完成判据

- `cd backend && ./.venv/bin/python -m pytest -q` 全绿,ruff clean(本 plan 引入代码 0 告警)。
- `backend/src/porto_chatbot/workflow_runner.py` 与 `backend/tests/test_workflow_runner.py` 已删除。
- API 契约不变:7 endpoints 路径 / response_model / 状态码 / `produced_by`/`produced_at` 审计字段全保留(`test_workflow_api.py` 全绿)。
- 持久化双层:`langgraph_checkpoints.sqlite` 独立于 `workflows.sqlite3`;`WorkflowStore` 仍管 `workflows` + `workflow_outputs`。
- 降级冒烟通过(无 key,graph 走 fallback 推进到 understand/evaluate)。
- 启动恢复:checkpoint 在 interrupt→awaiting_input;无 checkpoint→interrupted(保 current_step)。

## 后续(L3,本 plan 范围外)

依设计 §7 + L1 spike(U1/U2)另开 plan:
- spec refine 子图(initial/critique/decide/refine)做成独立子图;generate 节点用 `Send` map-reduce 并行扇出。
- U1(Send→reducer,L1 已验:wiring 用条件边 + 子图与父图共享 reducer 注解 key)、U2(sync Send 真并行,不需 ThreadPoolExecutor)已铺路。
- 届时删 generate 节点内的 `ThreadPoolExecutor` 并行块,换声明式 `Send`。
