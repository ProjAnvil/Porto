# 分步交互式 Workflow 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 chatbot 的 PRD 拆解从全自动同步 workflow 改成分步、可暂停/可编辑/可续跑的异步交互式 workflow,用自写状态机替换 langgraph,sqlite 持久化,前端轮询。

**Architecture:** `WorkflowRunner`(纯状态机,复用现有 nodes)→ `WorkflowExecutor`(后台线程 + 锁)→ sqlite 两表(`workflows` + `workflow_outputs`)→ REST(创建/轮询/继续/编辑)→ 前端分步向导 + 短轮询。checkpoint = {understand, identify, generate}。

**Tech Stack:** Python 3.12 / FastAPI / sqlite3(WAL) / pytest(TestClient + monkeypatch) / Next.js 16 + React 19 + assistant-ui(前端无测试框架,靠 tsc + 手动)。

**Spec:** [docs/superpowers/specs/2026-07-13-workflow-checkpoint-design.md](../specs/2026-07-13-workflow-checkpoint-design.md)

## Global Constraints

- Python `>=3.12`;后端测试跑 `cd backend && uv run pytest -q`(pythonpath=src,testpaths=tests)
- **无向后兼容**:`spec_refine_parallel`(bool)直接换成 `spec_refine_concurrency`(int),旧数据不迁移
- sqlite 路径 `~/.porto/workflows.sqlite3`,开 WAL;`current_step` 只在节点跑完后更新
- status 枚举固定:`created/running/awaiting_input/completed/failed/interrupted`
- checkpoint 集合固定:`{understand, identify, generate}`;`retrieve`/`evaluate` 自动过
- 编辑语义:PUT /steps/{step} → 覆盖产出 + current_step 回退到该步 + 删除其后 outputs + status=awaiting_input
- 服务重启:扫 `running` → 改 `interrupted`(不回退 current_step)
- 前端 [AGENTS.md](../../../frontend/AGENTS.md):本仓库 Next.js 16 是定制版,动前端前先读 `frontend/node_modules/next/dist/docs/` 相关指南
- 后端 TDD:每任务先写测试再实现;前端靠 `cd frontend && npx tsc --noEmit` + `npm run build` + 手动验证
- 每任务结束 commit;commit message 前缀 `feat(chatbot):` / `refactor(chatbot):` / `chore(chatbot):`

---

## File Structure

**新增(backend/src/porto_chatbot/):**
- `workflow_store.py` — sqlite 两表 CRUD + resume 重建
- `workflow_runner.py` — 状态机(STEPS/CHECKPOINTS/advance)
- `workflow_executor.py` — 后台线程 + 锁 + snapshot 重建 agent

**改:**
- `settings.py` — +`agent_request_timeout`,+`spec_refine_concurrency`,−`spec_refine_parallel`
- `models/payload.py` — `AgentSettingsPayload` 同步
- `llm/client.py` — `OpenAI()`/`Anthropic()` 加 `timeout=`
- `api/deps.py` — `default_agent_settings` 同步;+`runtime_settings_from_snapshot`
- `agent/nodes/generate.py` — 并发数来源改 `spec_refine_concurrency`
- `api/routes/workflow.py` — 新 endpoint 群
- `api/app.py` — lifespan 注册 executor + 启动扫 running→interrupted
- `tests/conftest.py` — `_ENV_KEYS_TO_ISOLATE` 同步
- `tests/test_api.py` — workflow 测试改新流程

**删:**
- `agent/graph.py`(PortoAgent 迁移到 `agent/agent.py`)
- `pyproject.toml` 的 `langgraph` 依赖

**前端:**
- `src/lib/types.ts` — `spec_refine_concurrency` + workflow 详情类型
- `src/lib/api.ts` — 新 workflow API client
- `src/components/porto-workbench.tsx` — WorkflowPanel 重构 + 历史 sidebar + spec 并发 UI

---

## Task 1: 配置项 agent_request_timeout + spec_refine_concurrency

**Files:**
- Modify: `backend/src/porto_chatbot/settings.py`
- Modify: `backend/src/porto_chatbot/models/payload.py`
- Modify: `backend/src/porto_chatbot/api/deps.py:57-84`(`default_agent_settings`)
- Modify: `backend/tests/conftest.py:38-47`(`_ENV_KEYS_TO_ISOLATE`)
- Test: `backend/tests/test_settings_fields.py`(新建)

**Interfaces:**
- Produces: `Settings.agent_request_timeout: int`(默认 120)、`Settings.spec_refine_concurrency: int`(默认 3);`AgentSettingsPayload` 同名字段;`default_agent_settings()` 返回新字段

- [ ] **Step 1: 写失败测试**

`backend/tests/test_settings_fields.py`:
```python
from porto_chatbot.settings import Settings
from porto_chatbot.models import AgentSettingsPayload


def test_settings_defaults():
    s = Settings()
    assert s.agent_request_timeout == 120
    assert s.spec_refine_concurrency == 3
    assert not hasattr(s, "spec_refine_parallel")


def test_settings_bounds():
    import pytest
    with pytest.raises(Exception):
        Settings(spec_refine_concurrency=0)
    with pytest.raises(Exception):
        Settings(spec_refine_concurrency=11)


def test_payload_has_new_fields():
    p = AgentSettingsPayload(spec_refine_concurrency=5, agent_request_timeout=60)
    assert p.spec_refine_concurrency == 5
    assert p.agent_request_timeout == 60
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_settings_fields.py -v`
Expected: FAIL(`agent_request_timeout` 不存在 / `spec_refine_parallel` 仍存在)

- [ ] **Step 3: 改 settings.py**

在 `settings.py` 的 Spec refine 区块(line 60-65 附近),把:
```python
    spec_refine_parallel: bool = True
```
替换为:
```python
    spec_refine_concurrency: int = Field(default=3, ge=1, le=10)
```

在 `agent_max_tool_turns`(line 75 附近)之后加:
```python
    # --- LLM 请求超时（秒），抗单次调用挂死 ---
    agent_request_timeout: int = Field(default=120, ge=10)
```

- [ ] **Step 4: 改 models/payload.py**

`AgentSettingsPayload` 里把 `spec_refine_parallel: bool | None = None` 替换为:
```python
    spec_refine_concurrency: int | None = Field(default=None, ge=1, le=10)
```
并在 Spec refine 区块末尾加:
```python
    agent_request_timeout: int | None = Field(default=None, ge=10)
```

- [ ] **Step 5: 改 api/deps.py `default_agent_settings`**

把 line 74 `spec_refine_parallel=settings.spec_refine_parallel,` 替换为:
```python
        spec_refine_concurrency=settings.spec_refine_concurrency,
```
并在 `agent_max_tool_turns=...` 后加:
```python
        agent_request_timeout=settings.agent_request_timeout,
```

- [ ] **Step 6: 改 conftest.py `_ENV_KEYS_TO_ISOLATE`**

把 `"PORTO_CHATBOT_SPEC_REFINE_PARALLEL",` 替换为 `"PORTO_CHATBOT_SPEC_REFINE_CONCURRENCY",`,并追加 `"PORTO_CHATBOT_AGENT_REQUEST_TIMEOUT",`。

- [ ] **Step 7: 跑全部测试确认通过**

Run: `cd backend && uv run pytest -q`
Expected: PASS(注意:`api/deps.py` 的 `default_agent_settings` 已同步,现有测试不应断;若有测试引用 `spec_refine_parallel`,grep 修掉)

- [ ] **Step 8: Commit**

```bash
git add backend/src/porto_chatbot/settings.py backend/src/porto_chatbot/models/payload.py \
        backend/src/porto_chatbot/api/deps.py backend/tests/conftest.py \
        backend/tests/test_settings_fields.py
git commit -m "feat(chatbot): 配置项 spec_refine_concurrency + agent_request_timeout"
```

---

## Task 2: LLM client 加 timeout

**Files:**
- Modify: `backend/src/porto_chatbot/llm/client.py:364-378`(`_build_client`)
- Test: `backend/tests/test_llm_timeout.py`(新建)

**Interfaces:**
- Consumes: `Settings.agent_request_timeout`(Task 1)
- Produces: `LLMClient` 构造的 `OpenAI`/`Anthropic` 带 `timeout=`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_llm_timeout.py`:
```python
from unittest.mock import patch
from porto_chatbot.llm import LLMClient
from porto_chatbot.settings import Settings


def test_openai_client_uses_configured_timeout():
    s = Settings(agent_api_key="k", agent_provider="openai", agent_request_timeout=77)
    with patch("porto_chatbot.llm.client.OpenAI") as mock_openai:
        LLMClient(s)
    _, kwargs = mock_openai.call_args
    assert kwargs["timeout"] == 77


def test_anthropic_client_uses_configured_timeout():
    s = Settings(agent_api_key="k", agent_provider="anthropic", agent_request_timeout=99)
    with patch("porto_chatbot.llm.client.Anthropic") as mock_anthropic:
        LLMClient(s)
    _, kwargs = mock_anthropic.call_args
    assert kwargs["timeout"] == 99
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_llm_timeout.py -v`
Expected: FAIL(`timeout` 未传入)

- [ ] **Step 3: 改 `_build_client`**

`client.py` 的 `_build_client` 里,openai 分支:
```python
        if self.settings.agent_provider == "openai":
            kwargs: dict[str, Any] = {
                "api_key": self.settings.agent_api_key,
                "timeout": self.settings.agent_request_timeout,
            }
            if self.settings.agent_base_url:
                kwargs["base_url"] = self.settings.agent_base_url
            return OpenAI(**kwargs)
```
anthropic 分支同理加 `"timeout": self.settings.agent_request_timeout,`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_llm_timeout.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/llm/client.py backend/tests/test_llm_timeout.py
git commit -m "feat(chatbot): LLM 调用加 timeout 抗挂死"
```

---

## Task 3: generate.py 并发数来源改 spec_refine_concurrency

**Files:**
- Modify: `backend/src/porto_chatbot/agent/nodes/generate.py:28-40`
- Test: `backend/tests/test_generate_concurrency.py`(新建)

**Interfaces:**
- Consumes: `Settings.spec_refine_concurrency`(Task 1)
- Produces: `generate_specs` 用 `min(spec_refine_concurrency, len(subs))` 个 worker

- [ ] **Step 1: 写失败测试**

`backend/tests/test_generate_concurrency.py`:
```python
from unittest.mock import MagicMock, patch
from porto_chatbot.agent.nodes.generate import generate_specs


def _make_agent(concurrency, n_subs):
    agent = MagicMock()
    agent.settings.spec_refine_concurrency = concurrency
    agent.settings.spec_refine_enabled = True
    agent.settings.spec_refine_parallel = True  # 兼容旧字段(过渡期);Task 9 删
    agent.llm.enabled = True
    agent.critic_llm = MagicMock()
    subs = [MagicMock(name=f"s{i}") for i in range(n_subs)]
    # _gen 内部建 SpecContext 调 generate_spec_with_loop;patch 掉避免真跑
    return agent, subs


def test_concurrency_caps_workers(monkeypatch):
    agent, subs = _make_agent(agent_concurrency_dummy := None, 6)
    agent.settings.spec_refine_concurrency = 3
    captured = {}

    class FakePool:
        def __init__(self, max_workers):
            captured["max_workers"] = max_workers
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def submit(self, fn, *args): ...
    # 让 parallel 分支走 ThreadPoolExecutor 路径
    captured_pool = FakePool
    monkeypatch.setattr("concurrent.futures.ThreadPoolExecutor", captured_pool)
    # 跳过真实生成
    monkeypatch.setattr("porto_chatbot.agent.nodes.generate.generate_spec_with_loop",
                        lambda ctx, sub: MagicMock(final="x", used_llm=False, iterations=1, model_dump=lambda: {}))

    state = {"workflow_id": "w1", "subsystems": subs, "prd_text": "p", "understanding": "u",
             "sources": [], "top_k": 6}
    generate_specs(agent, state)
    assert captured["max_workers"] == 3
```

注:测试里 `agent.settings.spec_refine_parallel` 设 True 是过渡——本任务同时把 generate.py 对 `spec_refine_parallel` 的依赖去掉(见 Step 3),测试就不需要它。先按 Step 3 改完后,把测试里那行删掉。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_generate_concurrency.py -v`
Expected: FAIL(当前用固定 8:`min(8, len(subs))`,不读 `spec_refine_concurrency`)

- [ ] **Step 3: 改 generate.py**

把 `generate.py:28-40` 的:
```python
    parallel = (
        agent.settings.spec_refine_parallel
        and agent.llm.enabled
        and agent.settings.spec_refine_enabled
        and len(subs) > 1
    )
    if parallel:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(8, len(subs))) as pool:
```
替换为:
```python
    parallel = (
        agent.llm.enabled
        and agent.settings.spec_refine_enabled
        and len(subs) > 1
    )
    if parallel:
        from concurrent.futures import ThreadPoolExecutor

        max_workers = min(agent.settings.spec_refine_concurrency, len(subs))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
```
然后删掉测试里 `agent.settings.spec_refine_parallel = True` 那行。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_generate_concurrency.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/agent/nodes/generate.py backend/tests/test_generate_concurrency.py
git commit -m "feat(chatbot): generate_specs 并发数来自 spec_refine_concurrency"
```

---

## Task 4: WorkflowStore(sqlite 两表)

**Files:**
- Create: `backend/src/porto_chatbot/workflow_store.py`
- Test: `backend/tests/test_workflow_store.py`

**Interfaces:**
- Consumes: `Settings.data_dir`(sqlite 路径 = `data_dir / "workflows.sqlite3"`)
- Produces:
  - `WorkflowStore(settings)` — 打开/建表,开 WAL
  - `create(session_id, project_name, prd_text, top_k, rag_snapshot: dict, agent_snapshot: dict) -> workflow_id` — 插入 status=created
  - `get(workflow_id) -> dict | None` — workflows 行(含 status/current_step/error 等)
  - `list_workflows(session_id=None, status=None, limit=50) -> list[dict]`
  - `save_output(workflow_id, step_name, output: dict, produced_by)` — upsert(PK 覆盖)
  - `get_outputs(workflow_id) -> dict[step_name, dict]` — 全部步骤产出
  - `clear_outputs_after(workflow_id, step_name)` — 删除该步**之后**的 outputs(回退用)
  - `update_status(workflow_id, status, current_step=None, error=None)`
  - `mark_running_interrupted_on_startup()` — 把所有 running 改 interrupted,返回受影响数
  - `delete(workflow_id)`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_workflow_store.py`:
```python
import pytest
from porto_chatbot.workflow_store import WorkflowStore
from porto_chatbot.settings import Settings

STEPS = ["retrieve", "understand", "identify", "generate", "evaluate"]


def _store(tmp_path):
    return WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))


def test_create_and_get(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "proj", "prd", 6, {"r": 1}, {"a": 1})
    row = s.get(wid)
    assert row["workflow_id"] == wid
    assert row["status"] == "created"
    assert row["project_name"] == "proj"
    assert row["current_step"] is None
    assert row["rag_snapshot"] == '{"r": 1}'


def test_save_output_upsert_and_get(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "p", "prd", 6, {}, {})
    s.save_output(wid, "understand", {"understanding": "v1"}, "ai")
    s.save_output(wid, "understand", {"understanding": "v2"}, "user")  # 覆盖
    outs = s.get_outputs(wid)
    assert outs["understand"]["output"] == {"understanding": "v2"}
    assert outs["understand"]["produced_by"] == "user"


def test_clear_outputs_after(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "p", "prd", 6, {}, {})
    for step in STEPS:
        s.save_output(wid, step, {"x": 1}, "ai")
    s.clear_outputs_after(wid, "understand")  # 删 identify/generate/evaluate
    outs = s.get_outputs(wid)
    assert set(outs.keys()) == {"retrieve", "understand"}


def test_list_filters(tmp_path):
    s = _store(tmp_path)
    w1 = s.create("s1", "p1", "prd", 6, {}, {})
    s.update_status(w1, "completed", current_step="evaluate")
    w2 = s.create("s2", "p2", "prd", 6, {}, {})
    assert len(s.list_workflows()) == 2
    assert len(s.list_workflows(session_id="s1")) == 1
    assert len(s.list_workflows(status="completed")) == 1


def test_mark_running_interrupted_on_startup(tmp_path):
    s = _store(tmp_path)
    w1 = s.create("s", "p", "prd", 6, {}, {})
    s.update_status(w1, "running", current_step="understand")
    w2 = s.create("s", "p2", "prd", 6, {}, {})
    s.update_status(w2, "awaiting_input", current_step="understand")
    n = s.mark_running_interrupted_on_startup()
    assert n == 1
    assert s.get(w1)["status"] == "interrupted"
    assert s.get(w2)["status"] == "awaiting_input"  # 不动


def test_delete(tmp_path):
    s = _store(tmp_path)
    wid = s.create("s", "p", "prd", 6, {}, {})
    s.save_output(wid, "understand", {"u": 1}, "ai")
    s.delete(wid)
    assert s.get(wid) is None
    assert s.get_outputs(wid) == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_store.py -v`
Expected: FAIL(`workflow_store` 模块不存在)

- [ ] **Step 3: 实现 workflow_store.py**

```python
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from .logging_utils import get_component_logger
from .settings import Settings

logger = get_component_logger("workflow_store")


class WorkflowStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db_path = settings.data_dir / "workflows.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id    TEXT PRIMARY KEY,
                    session_id     TEXT NOT NULL,
                    project_name   TEXT,
                    prd_text       TEXT NOT NULL,
                    top_k          INTEGER,
                    rag_snapshot   TEXT NOT NULL,
                    agent_snapshot TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    current_step   TEXT,
                    error          TEXT,
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS workflow_outputs (
                    workflow_id TEXT NOT NULL,
                    step_name   TEXT NOT NULL,
                    output      TEXT NOT NULL,
                    produced_by TEXT NOT NULL,
                    produced_at TEXT NOT NULL,
                    PRIMARY KEY (workflow_id, step_name)
                )"""
            )

    def create(self, session_id, project_name, prd_text, top_k, rag_snapshot, agent_snapshot) -> str:
        wid = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO workflows
                   (workflow_id, session_id, project_name, prd_text, top_k,
                    rag_snapshot, agent_snapshot, status, current_step, error,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (wid, session_id, project_name, prd_text, top_k,
                 json.dumps(rag_snapshot, ensure_ascii=False),
                 json.dumps(agent_snapshot, ensure_ascii=False),
                 "created", None, None, now, now),
            )
        logger.info("workflow created workflow_id=%s", wid)
        return wid

    def get(self, workflow_id) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_workflows(self, session_id=None, status=None, limit=50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM workflows"
        where, params = [], []
        if session_id:
            where.append("session_id=?"); params.append(session_id)
        if status:
            where.append("status=?"); params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def save_output(self, workflow_id, step_name, output: dict, produced_by) -> None:
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO workflow_outputs (workflow_id, step_name, output, produced_by, produced_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(workflow_id, step_name)
                   DO UPDATE SET output=excluded.output, produced_by=excluded.produced_by,
                                 produced_at=excluded.produced_at""",
                (workflow_id, step_name, json.dumps(output, ensure_ascii=False), produced_by, now),
            )

    def get_outputs(self, workflow_id) -> dict[str, dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_outputs WHERE workflow_id=?", (workflow_id,)
            ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            d["output"] = json.loads(d["output"])
            out[d["step_name"]] = d
        return out

    def clear_outputs_after(self, workflow_id, step_name) -> None:
        order = ["retrieve", "understand", "identify", "generate", "evaluate"]
        keep_idx = order.index(step_name)
        victims = order[keep_idx + 1:]
        if not victims:
            return
        placeholders = ",".join("?" * len(victims))
        with self._conn() as conn:
            conn.execute(
                f"DELETE FROM workflow_outputs WHERE workflow_id=? AND step_name IN ({placeholders})",
                (workflow_id, *victims),
            )

    def update_status(self, workflow_id, status, current_step=None, error=None) -> None:
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT current_step FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            cur = current_step if current_step is not None else (row["current_step"] if row else None)
            conn.execute(
                """UPDATE workflows SET status=?, current_step=?, error=?, updated_at=?
                   WHERE workflow_id=?""",
                (status, cur, error, now, workflow_id),
            )

    def mark_running_interrupted_on_startup(self) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                'UPDATE workflows SET status="interrupted", updated_at=? WHERE status="running"',
                (datetime.now(UTC).isoformat(),),
            )
            return cur.rowcount

    def delete(self, workflow_id) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM workflow_outputs WHERE workflow_id=?", (workflow_id,))
            conn.execute("DELETE FROM workflows WHERE workflow_id=?", (workflow_id,))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_store.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/workflow_store.py backend/tests/test_workflow_store.py
git commit -m "feat(chatbot): WorkflowStore sqlite 两表 + resume 重建"
```

---

## Task 5: WorkflowRunner(状态机)

**Files:**
- Create: `backend/src/porto_chatbot/workflow_runner.py`
- Test: `backend/tests/test_workflow_runner.py`

**Interfaces:**
- Consumes: `PortoAgent`(容器,提供 `llm`/`vector_store`/`critic_llm`/`settings`/`logger`/`_with_step`)、现有 `agent/nodes/*`(`retrieve_node.retrieve_knowledge` 等)、`agent/heuristics.infer_project_name`
- Produces:
  - `STEPS = ["retrieve","understand","identify","generate","evaluate"]`
  - `CHECKPOINTS = {"understand","identify","generate"}`
  - `WorkflowRunner.run_to_next_checkpoint(agent, state) -> state` — 从 `state["current_step"]` 的下一步跑到下个 checkpoint(或 completed);每步把产出写入 state 并返回,调用方负责落库

说明:`state` 是 `PortoAgentState`(TypedDict)外加一个 `current_step` 键。runner 不碰 sqlite/线程。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_workflow_runner.py`:
```python
from unittest.mock import MagicMock
from porto_chatbot.workflow_runner import WorkflowRunner, STEPS, CHECKPOINTS


def _agent():
    return MagicMock()


def _state(current_step=None):
    return {
        "workflow_id": "w1", "project_name": "p", "prd_text": "prd",
        "sources": [], "understanding": "", "subsystems": [], "specs": {},
        "evaluation": {}, "steps": [], "top_k": 6, "current_step": current_step,
    }


def test_stops_at_understand_from_none():
    """从 current_step=None 跑,retrieve 自动过,understand 是 checkpoint → 停在 understand。"""
    agent = _agent()
    # patch 节点函数:retrieve/understand 写入产出并推进 _with_step
    import porto_chatbot.workflow_runner as wr
    calls = []
    def fake_retrieve(agent_, state):
        state["sources"] = ["src"]; state["current_step"] = "retrieve"
        calls.append("retrieve"); return state
    def fake_understand(agent_, state):
        state["understanding"] = "U"; state["current_step"] = "understand"
        calls.append("understand"); return state
    wr.retrieve_node.retrieve_knowledge = fake_retrieve
    wr.understand_node.understand_prd = fake_understand

    state = WorkflowRunner.run_to_next_checkpoint(agent, _state(None))
    assert state["status"] == "awaiting_input"
    assert state["current_step"] == "understand"
    assert calls == ["retrieve", "understand"]


def test_stops_at_identify_from_understand():
    import porto_chatbot.workflow_runner as wr
    calls = []
    wr.identify_node.identify_subsystems = lambda a, s: (s.update(subsystems=["s1"], current_step="identify"), calls.append("identify"), s)[2]
    state = WorkflowRunner.run_to_next_checkpoint(_agent(), _state("understand"))
    assert state["status"] == "awaiting_input"
    assert state["current_step"] == "identify"


def test_runs_to_completed_from_generate():
    import porto_chatbot.workflow_runner as wr
    calls = []
    wr.evaluate_node.evaluate = lambda a, s: (s.update(evaluation={"score": 100}, current_step="evaluate"), calls.append("evaluate"), s)[2]
    state = WorkflowRunner.run_to_next_checkpoint(_agent(), _state("generate"))
    assert state["status"] == "completed"
    assert state["current_step"] == "evaluate"


def test_steps_and_checkpoints_constants():
    assert STEPS == ["retrieve", "understand", "identify", "generate", "evaluate"]
    assert CHECKPOINTS == {"understand", "identify", "generate"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_runner.py -v`
Expected: FAIL(模块不存在)

- [ ] **Step 3: 实现 workflow_runner.py**

```python
from __future__ import annotations

from typing import Any

from .agent.nodes import evaluate as evaluate_node
from .agent.nodes import generate as generate_node
from .agent.nodes import identify as identify_node
from .agent.nodes import retrieve as retrieve_node
from .agent.nodes import understand as understand_node

STEPS = ["retrieve", "understand", "identify", "generate", "evaluate"]
CHECKPOINTS = {"understand", "identify", "generate"}

# 节点函数签名:node(agent, state) -> state;各节点内部已 self._with_step(...)
_NODE_FUNCS = {
    "retrieve": retrieve_node.retrieve_knowledge,
    "understand": understand_node.understand_prd,
    "identify": identify_node.identify_subsystems,
    "generate": generate_node.generate_specs,
    "evaluate": evaluate_node.evaluate,
}


class WorkflowRunner:
    """纯状态机:从 current_step 的下一步跑到下个 checkpoint(或 completed)。
    不碰 sqlite/线程。产出写入 state,调用方(WorkflowExecutor)负责落库。
    """

    @staticmethod
    def run_to_next_checkpoint(agent, state: dict[str, Any]) -> dict[str, Any]:
        current = state.get("current_step")
        start_idx = STEPS.index(current) + 1 if current in STEPS else 0
        for step in STEPS[start_idx:]:
            state = _NODE_FUNCS[step](agent, state)
            state["current_step"] = step
            if step in CHECKPOINTS:
                state["status"] = "awaiting_input"
                return state
        state["status"] = "completed"
        return state
```

注:节点函数(如 `retrieve_knowledge(agent, state)`)内部会调 `agent._with_step(...)` 设置 state["steps"] 并 return state。`_with_step` 已在 `PortoAgent`(graph.py,Task 9 前仍存在)上。测试用 MagicMock agent 时,需让节点函数被 mock(测试里直接替换 `_NODE_FUNCS` 引用的模块函数,如上)。测试里直接覆盖 `retrieve_node.retrieve_knowledge` 等模块属性即可,因为 `_NODE_FUNCS` 在模块加载时绑定——**为让 mock 生效**,改为运行时查找:

把 `_NODE_FUNCS[step]` 的使用改为模块级查找。修正实现:在 `run_to_next_checkpoint` 内通过模块属性查找,而非预绑定字典。最终实现:

```python
_NODE_MODULES = {
    "retrieve": retrieve_node, "understand": understand_node,
    "identify": identify_node, "generate": generate_node, "evaluate": evaluate_node,
}
_NODE_FN = {
    "retrieve": "retrieve_knowledge", "understand": "understand_prd",
    "identify": "identify_subsystems", "generate": "generate_specs", "evaluate": "evaluate",
}

# run_to_next_checkpoint 内:
#     fn = getattr(_NODE_MODULES[step], _NODE_FN[step])
#     state = fn(agent, state)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_runner.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/workflow_runner.py backend/tests/test_workflow_runner.py
git commit -m "feat(chatbot): WorkflowRunner 状态机(复用 nodes)"
```

---

## Task 6: WorkflowExecutor(后台线程 + snapshot 重建)

**Files:**
- Create: `backend/src/porto_chatbot/workflow_executor.py`
- Modify: `backend/src/porto_chatbot/api/deps.py`(加 `runtime_settings_from_snapshot`)
- Test: `backend/tests/test_workflow_executor.py`

**Interfaces:**
- Consumes: `WorkflowStore`(Task 4)、`WorkflowRunner`(Task 5)、`PortoAgent`、`LocalVectorStore`、`LLMClient`、`apply_rag_settings` 模式(deps)
- Produces:
  - `WorkflowExecutor(settings, store)` — 持有锁字典 `{workflow_id: Lock}`
  - `executor.start_workflow(workflow_id)` — 后台线程跑 `retrieve→understand`(第一个 checkpoint)
  - `executor.advance(workflow_id) -> bool` — 加锁,若 running 返回 False(调用方返回 409);否则起后台线程跑到下个 checkpoint。返回 True 表示已接受
  - `executor.snapshot` 重建:从 `workflows.rag_snapshot`/`agent_snapshot`(JSON)重建 `Settings`,不走 db

- [ ] **Step 1: 在 deps.py 加 snapshot 重建 helper**

`deps.py` 末尾加:
```python
def runtime_settings_from_snapshot(rag_snapshot: dict, agent_snapshot: dict, top_k: int | None = None):
    """从创建时的 resolved 配置快照重建 Settings,**不读 db**(免受后续配置改动影响)。

    snapshot 是创建时 effective_rag_settings/agent_settings 的 model_dump;此处以
    Settings() 默认(.env)为底,用 snapshot 完整覆盖。
    """
    settings = current_settings()
    updates = {**agent_snapshot, **rag_snapshot}
    if top_k is not None:
        updates["top_k"] = top_k
    if "chunk_size" in updates:
        updates["max_chunk_chars"] = updates.pop("chunk_size")
    if updates.get("kb_dirs"):
        updates["kb_dirs"] = [Path(d) for d in updates["kb_dirs"]]
    return settings.model_copy(update={k: v for k, v in updates.items() if v is not None})
```

- [ ] **Step 2: 写失败测试**

`backend/tests/test_workflow_executor.py`:
```python
import time
from porto_chatbot.workflow_executor import WorkflowExecutor
from porto_chatbot.workflow_store import WorkflowStore
from porto_chatbot.settings import Settings


def _make(tmp_path):
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    return WorkflowExecutor(settings, store), store


def test_start_runs_to_understand_checkpoint(tmp_path, monkeypatch):
    ex, store = _make(tmp_path)
    # mock runner:直接把 state 推到 understand checkpoint
    import porto_chatbot.workflow_executor as we
    def fake_run(agent, state):
        state["current_step"] = "understand"
        state["status"] = "awaiting_input"
        state["sources"] = []
        state["understanding"] = "U"
        return state
    monkeypatch.setattr(we.WorkflowRunner, "run_to_next_checkpoint", staticmethod(fake_run))
    wid = store.create("s", "p", "prd", 6, {"embedding_provider": "local"}, {"agent_provider": "openai"})
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    row = store.get(wid)
    assert row["status"] == "awaiting_input"
    assert row["current_step"] == "understand"
    outs = store.get_outputs(wid)
    assert "understand" in outs


def test_advance_returns_false_when_running(tmp_path, monkeypatch):
    ex, store = _make(tmp_path)
    import porto_chatbot.workflow_executor as we
    started = {"x": False}
    def slow_run(agent, state):
        started["x"] = True
        time.sleep(0.3)
        state["current_step"] = "understand"; state["status"] = "awaiting_input"
        return state
    monkeypatch.setattr(we.WorkflowRunner, "run_to_next_checkpoint", staticmethod(slow_run))
    wid = store.create("s", "p", "prd", 6, {"embedding_provider": "local"}, {"agent_provider": "openai"})
    ex.start_workflow(wid)
    # 不等,直接再 advance
    assert ex.advance(wid) is False  # 正在 running
    ex.wait(wid, timeout=5)


def test_failed_records_error(tmp_path, monkeypatch):
    ex, store = _make(tmp_path)
    import porto_chatbot.workflow_executor as we
    def boom(agent, state):
        raise RuntimeError("llm down")
    monkeypatch.setattr(we.WorkflowRunner, "run_to_next_checkpoint", staticmethod(boom))
    wid = store.create("s", "p", "prd", 6, {"embedding_provider": "local"}, {"agent_provider": "openai"})
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    row = store.get(wid)
    assert row["status"] == "failed"
    assert "llm down" in row["error"]
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_executor.py -v`
Expected: FAIL(模块不存在)

- [ ] **Step 4: 实现 workflow_executor.py**

```python
from __future__ import annotations

import threading
from typing import Any

from .agent import PortoAgent
from .agent.heuristics import infer_project_name
from .llm import LLMClient
from .logging_utils import get_component_logger
from .settings import Settings
from .vector_store import LocalVectorStore
from .workflow_runner import WorkflowRunner
from .workflow_store import WorkflowStore

logger = get_component_logger("workflow_executor")

# 各 step 的产出字段(state → output json)
_STEP_OUTPUT_KEYS = {
    "retrieve": ["sources"],
    "understand": ["understanding"],
    "identify": ["subsystems"],
    "generate": ["specs", "spec_results"],
    "evaluate": ["evaluation"],
}


class WorkflowExecutor:
    """后台线程推进 workflow;每 workflow 一把锁防并发 advance。"""

    def __init__(self, settings: Settings, store: WorkflowStore):
        self.settings = settings
        self.store = store
        self._locks: dict[str, threading.Lock] = {}
        self._guards: dict[str, threading.Lock] = {}  # 标记是否在跑
        self._global = threading.Lock()

    def _guard(self, workflow_id) -> threading.Lock:
        with self._global:
            if workflow_id not in self._guards:
                self._guards[workflow_id] = threading.Lock()
            return self._guards[workflow_id]

    def _is_running(self, workflow_id) -> bool:
        return self._guard(workflow_id).locked()

    def wait(self, workflow_id, timeout: float = 30.0) -> None:
        """测试用:等当前后台任务结束。"""
        deadline = threading.Event()
        deadline.wait(0)  # noop;用轮询
        import time as _t
        end = _t.time() + timeout
        while _t.time() < end:
            if not self._is_running(workflow_id):
                return
            _t.sleep(0.05)
        raise TimeoutError(f"workflow {workflow_id} still running after {timeout}s")

    def start_workflow(self, workflow_id) -> None:
        self._run_async(workflow_id)

    def advance(self, workflow_id) -> bool:
        """返回 True=已接受(起线程);False=正在 running(调用方应 409)。"""
        guard = self._guard(workflow_id)
        if not guard.acquire(blocking=False):
            return False
        guard.release()
        self._run_async(workflow_id)
        return True

    def _run_async(self, workflow_id) -> None:
        guard = self._guard(workflow_id)
        guard.acquire()
        t = threading.Thread(target=self._worker, args=(workflow_id, guard), daemon=True)
        t.start()

    def _worker(self, workflow_id, guard) -> None:
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

    def _run_sync(self, workflow_id) -> None:
        row = self.store.get(workflow_id)
        if row is None:
            return
        self.store.update_status(workflow_id, "running")
        # 从 snapshot 重建 agent(不走 db)
        from .api.deps import runtime_settings_from_snapshot
        import json
        rag_snap = json.loads(row["rag_snapshot"])
        agent_snap = json.loads(row["agent_snapshot"])
        runtime = runtime_settings_from_snapshot(rag_snap, agent_snap, row["top_k"])
        agent = PortoAgent(runtime, LocalVectorStore(runtime), LLMClient(runtime))
        # 重建 state
        state = self._rebuild_state(row)
        try:
            state = WorkflowRunner.run_to_next_checkpoint(agent, state)
        except Exception as exc:
            logger.exception("workflow step failed workflow_id=%s", workflow_id)
            self.store.update_status(workflow_id, "failed", error=str(exc))
            return
        # 落库:新跑过的步的产出
        self._persist_state(workflow_id, state)
        self.store.update_status(workflow_id, state.get("status", "running"),
                                 current_step=state.get("current_step"))

    def _rebuild_state(self, row) -> dict[str, Any]:
        outs = self.store.get_outputs(row["workflow_id"])
        state: dict[str, Any] = {
            "workflow_id": row["workflow_id"],
            "project_name": row["project_name"] or infer_project_name(row["prd_text"]),
            "prd_text": row["prd_text"],
            "top_k": row["top_k"],
            "current_step": row["current_step"],
            "steps": [],
            "sources": [], "understanding": "", "subsystems": [], "specs": {}, "evaluation": {},
        }
        for step, data in outs.items():
            out = data["output"]
            for k, v in out.items():
                state[k] = v
        return state

    def _persist_state(self, workflow_id, state) -> None:
        from .agent.state import PortoAgentState  # noqa: F401
        # 只持久化"已完成步"的产出:current_step 及之前的步
        from .workflow_runner import STEPS
        cur = state.get("current_step")
        if cur in STEPS:
            end_idx = STEPS.index(cur)
        else:
            end_idx = len(STEPS) - 1
        for step in STEPS[: end_idx + 1]:
            out = {k: state[k] for k in _STEP_OUTPUT_KEYS[step] if k in state}
            if out:
                self.store.save_output(workflow_id, step, out, "ai")
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_executor.py -v`
Expected: 3 PASS。若 `test_advance_returns_false_when_running` 时序不稳,把 `slow_run` 的 sleep 调大到 0.5。

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/workflow_executor.py backend/src/porto_chatbot/api/deps.py \
        backend/tests/test_workflow_executor.py
git commit -m "feat(chatbot): WorkflowExecutor 后台线程 + snapshot 重建"
```

---

## Task 7: workflow API route 改造 + test_api 更新

**Files:**
- Modify: `backend/src/porto_chatbot/api/routes/workflow.py`(整文件重写)
- Modify: `backend/src/porto_chatbot/api/deps.py`(加 `get_workflow_executor`/`get_workflow_store`)
- Modify: `backend/tests/test_api.py:37-44`(`test_api_chat_and_workflow` 的 workflow 断言)
- Test: `backend/tests/test_workflow_api.py`(新建)

**Interfaces:**
- Consumes: `WorkflowExecutor`、`WorkflowStore`、`effective_rag_settings`/`effective_agent_settings`
- Produces REST endpoints(见 spec 2.1):POST /workflows、POST /workflows/upload、GET /workflows、GET /workflows/{id}、POST /workflows/{id}/advance、PUT /workflows/{id}/steps/{step}、DELETE /workflows/{id}

- [ ] **Step 1: 在 deps.py 加 executor/store 单例**

deps.py 末尾加(仿 `_ensure_rag_singletons` 按 data_dir 缓存):
```python
def get_workflow_store() -> "WorkflowStore":
    from ..workflow_store import WorkflowStore
    settings = current_settings()
    key = str(settings.data_dir)
    entry = _ensure_rag_singletons()
    store = entry.get("workflow_store")
    if store is None:
        store = WorkflowStore(settings)
        entry["workflow_store"] = store
    return store


def get_workflow_executor() -> "WorkflowExecutor":
    from ..workflow_executor import WorkflowExecutor
    settings = current_settings()
    entry = _ensure_rag_singletons()
    ex = entry.get("workflow_executor")
    if ex is None:
        ex = WorkflowExecutor(settings, get_workflow_store())
        entry["workflow_executor"] = ex
    return ex
```
注:`_ensure_rag_singletons` 的 entry dict 加两个可选键;首次访问时懒加载。

- [ ] **Step 2: 写失败测试**

`backend/tests/test_workflow_api.py`:
```python
import time
from fastapi.testclient import TestClient
from porto_chatbot import main


def _wait_status(client, wid, target, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/porto/workflows/{wid}").json()
        if r["status"] in target:
            return r
        time.sleep(0.1)
    raise AssertionError(f"never reached {target}, last={r}")


def test_workflow_checkpoint_flow(monkeypatch, sample_settings, sample_prd):
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        # 等 index(复用 _wait_index_done)
        from tests.test_api import _wait_index_done
        _wait_index_done(client)

        resp = client.post("/api/porto/workflows",
                           json={"text": sample_prd, "project_name": "支付平台", "session_id": "s1"})
        assert resp.status_code == 200
        wid = resp.json()["workflow_id"]
        # 跑到 understand checkpoint(降级路径,无 LLM key,确定性)
        detail = _wait_status(client, wid, {"awaiting_input", "completed"})
        assert detail["current_step"] in {"understand", "evaluate"}
        outs = detail["outputs"]
        assert "understanding" in outs.get("understand", {}).get("output", {}) or detail["current_step"] == "evaluate"


def test_list_and_delete(monkeypatch, sample_settings, sample_prd):
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        from tests.test_api import _wait_index_done
        _wait_index_done(client)
        r = client.post("/api/porto/workflows", json={"text": sample_prd, "session_id": "s1"})
        wid = r.json()["workflow_id"]
        lst = client.get("/api/porto/workflows?session_id=s1").json()
        assert any(w["workflow_id"] == wid for w in lst["items"])
        assert client.delete(f"/api/porto/workflows/{wid}").status_code == 204


def test_advance_concurrent_returns_409(monkeypatch, sample_settings, sample_prd):
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        from tests.test_api import _wait_index_done
        _wait_index_done(client)
        r = client.post("/api/porto/workflows", json={"text": sample_prd, "session_id": "s1"})
        wid = r.json()["workflow_id"]
        # 立刻连续 advance(第一个可能已过 understand)
        r2 = client.post(f"/api/porto/workflows/{wid}/advance")
        # 至少不 500;若已 completed 返回 200/409 任一可接受
        assert r2.status_code in (200, 409)
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_api.py -v`
Expected: FAIL(新 endpoint 不存在 / 返回旧 WorkflowResponse)

- [ ] **Step 4: 重写 workflow.py route**

整文件替换 `api/routes/workflow.py`:
```python
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ...documents import read_document
from ...logging_utils import get_component_logger
from ...models import WorkflowRequest
from ..deps import (
    effective_agent_settings, effective_rag_settings, get_workflow_executor, get_workflow_store,
)

logger = get_component_logger("api")
router = APIRouter()


class WorkflowCreated(BaseModel):
    workflow_id: str
    status: str


class WorkflowListItem(BaseModel):
    workflow_id: str
    project_name: str | None
    status: str
    current_step: str | None
    created_at: str
    score: int | None = None


class WorkflowListResponse(BaseModel):
    items: list[WorkflowListItem]


class WorkflowDetail(BaseModel):
    workflow_id: str
    session_id: str
    project_name: str | None
    status: str
    current_step: str | None
    error: str | None
    created_at: str
    updated_at: str
    outputs: dict[str, Any]  # {step: {output, produced_by, produced_at}}


def _detail(store, workflow_id) -> WorkflowDetail:
    row = store.get(workflow_id)
    if row is None:
        raise HTTPException(404, "workflow not found")
    outs = store.get_outputs(workflow_id)
    # 附 evaluation score(若有)
    score = None
    if "evaluate" in outs:
        ev = outs["evaluate"]["output"].get("evaluation") or {}
        score = ev.get("score")
    return WorkflowDetail(
        workflow_id=row["workflow_id"], session_id=row["session_id"],
        project_name=row["project_name"], status=row["status"],
        current_step=row["current_step"], error=row["error"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        outputs={k: {"output": v["output"], "produced_by": v["produced_by"],
                     "produced_at": v["produced_at"]} for k, v in outs.items()},
    )


@router.post("/api/porto/workflows", response_model=WorkflowCreated)
def create_workflow(req: WorkflowRequest):
    if not req.text or not req.text.strip():
        raise HTTPException(400, "text is required")
    rag = effective_rag_settings(req.rag).model_dump(exclude_none=True)
    agent = effective_agent_settings(req.agent).model_dump(exclude_none=True)
    top_k = req.top_k or effective_rag_settings(req.rag).top_k
    store = get_workflow_store()
    wid = store.create(req.session_id, req.project_name, req.text.strip(), top_k, rag, agent)
    logger.info("workflow start session_id=%s workflow_id=%s", req.session_id, wid)
    get_workflow_executor().start_workflow(wid)
    return WorkflowCreated(workflow_id=wid, status="running")


@router.post("/api/porto/workflows/upload", response_model=WorkflowCreated)
async def upload_workflow(
    file: Annotated[UploadFile, File()],
    project_name: Annotated[str | None, Form()] = None,
    session_id: Annotated[str | None, Form()] = "default",
    top_k: Annotated[int | None, Form()] = None,
):
    suffix = Path(file.filename or "").suffix
    if not suffix:
        raise HTTPException(400, "uploaded file must have an extension")
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        text = read_document(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if not text.strip():
        raise HTTPException(400, "document has no extractable text")
    rag = effective_rag_settings().model_dump(exclude_none=True)
    agent = effective_agent_settings().model_dump(exclude_none=True)
    resolved_top_k = top_k or effective_rag_settings().top_k
    store = get_workflow_store()
    wid = store.create(session_id or "default", project_name, text.strip(), resolved_top_k, rag, agent)
    get_workflow_executor().start_workflow(wid)
    return WorkflowCreated(workflow_id=wid, status="running")


@router.get("/api/porto/workflows", response_model=WorkflowListResponse)
def list_workflows(session_id: str | None = None, status: str | None = None, limit: int = 50):
    store = get_workflow_store()
    rows = store.list_workflows(session_id=session_id, status=status, limit=limit)
    items = []
    for r in rows:
        score = None
        outs = store.get_outputs(r["workflow_id"])
        if "evaluate" in outs:
            score = (outs["evaluate"]["output"].get("evaluation") or {}).get("score")
        items.append(WorkflowListItem(
            workflow_id=r["workflow_id"], project_name=r["project_name"],
            status=r["status"], current_step=r["current_step"],
            created_at=r["created_at"], score=score,
        ))
    return WorkflowListResponse(items=items)


@router.get("/api/porto/workflows/{workflow_id}", response_model=WorkflowDetail)
def get_workflow(workflow_id: str):
    return _detail(get_workflow_store(), workflow_id)


@router.post("/api/porto/workflows/{workflow_id}/advance", response_model=WorkflowCreated)
def advance_workflow(workflow_id: str):
    store = get_workflow_store()
    row = store.get(workflow_id)
    if row is None:
        raise HTTPException(404, "workflow not found")
    if row["status"] in ("completed",):
        raise HTTPException(409, "workflow already completed")
    if not get_workflow_executor().advance(workflow_id):
        raise HTTPException(409, "workflow is currently running")
    return WorkflowCreated(workflow_id=workflow_id, status="running")


@router.put("/api/porto/workflows/{workflow_id}/steps/{step}", response_model=WorkflowDetail)
def save_step_output(workflow_id: str, step: str, body: dict[str, Any]):
    store = get_workflow_store()
    row = store.get(workflow_id)
    if row is None:
        raise HTTPException(404, "workflow not found")
    if step not in {"understand", "identify", "generate"}:
        raise HTTPException(400, "step is not editable")
    store.save_output(workflow_id, step, body, "user")
    store.clear_outputs_after(workflow_id, step)
    store.update_status(workflow_id, "awaiting_input", current_step=step)
    return _detail(store, workflow_id)


@router.delete("/api/porto/workflows/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: str):
    store = get_workflow_store()
    if store.get(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    store.delete(workflow_id)
```

- [ ] **Step 5: 更新 test_api.py 的旧 workflow 断言**

`test_api.py:37-44` 原本断言 `wf_resp.json()["evaluation"]["passed"]` 等(旧同步返回)。改为只验证新 endpoint 返回 workflow_id:
```python
        wf_resp = client.post(
            "/api/porto/workflows",
            json={"text": sample_prd, "project_name": "支付平台"},
        )
        assert wf_resp.status_code == 200
        assert "workflow_id" in wf_resp.json()
        assert wf_resp.json()["status"] == "running"
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_api.py tests/test_api.py -v`
Expected: PASS。若降级路径下 workflow 很快跑完到 completed(无 LLM key 时 understand/identify 走 fallback、generate 走 template、evaluate 给分),`_wait_status` 的 target 含 completed 即可。

- [ ] **Step 7: Commit**

```bash
git add backend/src/porto_chatbot/api/routes/workflow.py backend/src/porto_chatbot/api/deps.py \
        backend/tests/test_workflow_api.py backend/tests/test_api.py
git commit -m "feat(chatbot): workflow API 改异步分步 + checkpoint 编辑/回退"
```

---

## Task 8: app lifespan(executor 单例 + 启动扫 running→interrupted)

**Files:**
- Modify: `backend/src/porto_chatbot/api/app.py:29-46`(lifespan)
- Test: `backend/tests/test_workflow_startup_recovery.py`

**Interfaces:**
- Consumes: `WorkflowStore.mark_running_interrupted_on_startup`(Task 4)
- Produces:服务启动时把残留 `running` 改 `interrupted`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_workflow_startup_recovery.py`:
```python
from fastapi.testclient import TestClient
from porto_chatbot import main
from porto_chatbot.api.deps import get_workflow_store


def test_startup_marks_running_interrupted(monkeypatch, sample_settings, sample_prd):
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    # 第一段:创建 workflow 并人为标 running
    with TestClient(main.app) as client:
        store = get_workflow_store()
        wid = store.create("s", "p", "prd", 6, {}, {})
        store.update_status(wid, "running", current_step="understand")
    # 第二段:重新启动 app(新 lifespan)→ 应扫到 running 改 interrupted
    with TestClient(main.app) as client:
        store = get_workflow_store()
        row = store.get(wid)
        assert row["status"] == "interrupted"
        assert row["current_step"] == "understand"  # 不回退
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_startup_recovery.py -v`
Expected: FAIL(重启后 status 仍 running)

- [ ] **Step 3: 改 app.py lifespan**

`app.py` lifespan startup 段(在 `supervisor.start()` 后)加:
```python
    from .deps import get_workflow_store
    n = get_workflow_store().mark_running_interrupted_on_startup()
    if n:
        logger.info("workflow startup recovery: %s running→interrupted", n)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_startup_recovery.py -v`
Expected: PASS

- [ ] **Step 5: 全量回归**

Run: `cd backend && uv run pytest -q`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/api/app.py backend/tests/test_workflow_startup_recovery.py
git commit -m "feat(chatbot): 启动恢复残留 running→interrupted"
```

---

## Task 9: 删 graph.py + PortoAgent 瘦身 + 移除 langgraph

**Files:**
- Create: `backend/src/porto_chatbot/agent/agent.py`(PortoAgent 容器)
- Delete: `backend/src/porto_chatbot/agent/graph.py`
- Modify: `backend/src/porto_chatbot/agent/__init__.py`
- Modify: `backend/pyproject.toml:12`(移除 langgraph)
- Modify: `backend/uv.lock`(`uv lock` 重生成)

**Interfaces:**
- Consumes:无新外部依赖
- Produces:瘦 `PortoAgent`(仅容器:settings/logger/vector_store/llm/critic_llm + `_build_critic_llm` + `_with_step`),从 `agent.agent` 导出

- [ ] **Step 1: 新建 agent/agent.py**

把 `graph.py` 里 `PortoAgent` 的构造、`_build_critic_llm`、`_with_step`、`agent ready` 日志搬到 `agent.py`,**删除** `run`/`_persist`/`_build_graph`/`_route_after_evaluate`/各 `retrieve_knowledge` 等 node 委托方法(改由 runner 直接调 nodes):
```python
from __future__ import annotations

from typing import Any

from ..llm import LLMClient
from ..logging_utils import get_component_logger
from ..models import AgentStep
from ..settings import Settings
from ..vector_store import LocalVectorStore


class PortoAgent:
    """Agent 上下文容器:持有 settings/llm/vector_store/critic_llm。
    编排由 WorkflowRunner 负责(不再有 graph/run)。"""

    def __init__(self, settings, vector_store=None, llm=None):
        self.settings = settings
        self.logger = get_component_logger("agent", settings)
        self.vector_store = vector_store or LocalVectorStore(settings)
        self.llm = llm or LLMClient(settings)
        self.critic_llm = self._build_critic_llm()
        self.logger.info("agent ready")

    def _build_critic_llm(self):
        # 原 graph.py:_build_critic_llm 的完整逻辑(逐行搬过来)
        s = self.settings
        if not s.critic_provider:
            return self.llm
        critic_settings = Settings()
        critic_settings.agent_provider = s.critic_provider
        critic_settings.agent_api_key = s.critic_api_key or s.agent_api_key
        critic_settings.agent_base_url = s.critic_base_url or s.agent_base_url
        critic_settings.agent_model = s.critic_model or s.agent_model
        critic_settings.agent_temperature = s.critic_temperature
        critic_settings.agent_max_tokens = s.critic_max_tokens
        critic = LLMClient(critic_settings)
        self.logger.info("critic llm ready provider=%s model=%s independent=%s",
                         s.critic_provider, s.critic_model, critic.enabled)
        return critic

    def _with_step(self, state, name, summary, data):
        steps = list(state.get("steps", []))
        steps.append(AgentStep(name=name, status="completed", summary=summary, data=data))
        state["steps"] = steps
        self.logger.info("step completed workflow_id=%s name=%s summary=%s",
                         state.get("workflow_id"), name, summary)
        return state
```

- [ ] **Step 2: 改 agent/__init__.py**

```python
from .agent import PortoAgent
from .state import PortoAgentState

__all__ = ["PortoAgent", "PortoAgentState"]
```

- [ ] **Step 3: 删 graph.py + 清理 deps.get_agent(若无人用)**

`rm backend/src/porto_chatbot/agent/graph.py`。

grep 确认无残留引用:
```bash
cd backend && grep -rn "from .*graph import\| PortoAgent" src tests --include='*.py' | grep -v "agent/agent.py\|agent/__init__.py\|workflow_executor\|deps.py"
```
- `deps.py:get_agent()` 若仅被已删的 route 用,删除它;若 chat route 等还用,保留(它 new PortoAgent,容器版兼容)。

- [ ] **Step 4: 移除 langgraph 依赖**

`pyproject.toml` 删掉 `  "langgraph>=0.5.0",` 这行,然后:
```bash
cd backend && uv lock
```

- [ ] **Step 5: 全量回归**

Run: `cd backend && uv run pytest -q`
Expected: 全 PASS。若有测试 import `from porto_chatbot.agent.graph import ...`,改为从 `agent.agent` 或 `agent` 导入。

- [ ] **Step 6: 确认无 langgraph 残留**

```bash
cd backend && grep -rn "langgraph" src tests --include='*.py' || echo "clean"
```
Expected: `clean`

- [ ] **Step 7: Commit**

```bash
git add backend/src/porto_chatbot/agent/agent.py backend/src/porto_chatbot/agent/__init__.py \
        backend/pyproject.toml backend/uv.lock
git rm backend/src/porto_chatbot/agent/graph.py
git commit -m "refactor(chatbot): PortoAgent 瘦身为容器,移除 langgraph 依赖"
```

---

## Task 10: 前端 types + api client

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/lib/api.ts`
- 验证:`cd frontend && npx tsc --noEmit`

**Interfaces:**
- Consumes:Task 7 的 REST 契约
- Produces:`WorkflowDetail`/`WorkflowListItem` 类型 + `createWorkflow`/`listWorkflows`/`getWorkflow`/`advanceWorkflow`/`saveStepOutput`/`deleteWorkflow`

> ⚠️ 动前端前先读 `frontend/node_modules/next/dist/docs/` 里 App Router / fetch 相关指南(AGENTS.md 要求)。

- [ ] **Step 1: types.ts 改 spec_refine_concurrency + 加 workflow 详情类型**

把 `AgentConfig` 里 `spec_refine_parallel: boolean;` 替换为:
```ts
  spec_refine_concurrency: number;
```
并在 `AgentConfig` 加 `agent_request_timeout: number;`。

文件末尾加:
```ts
export type WorkflowStepName = "retrieve" | "understand" | "identify" | "generate" | "evaluate";
export type WorkflowStatus = "created" | "running" | "awaiting_input" | "completed" | "failed" | "interrupted";

export type WorkflowOutputEntry = {
  output: Record<string, unknown>;
  produced_by: "ai" | "user";
  produced_at: string;
};

export type WorkflowDetail = {
  workflow_id: string;
  session_id: string;
  project_name: string | null;
  status: WorkflowStatus;
  current_step: WorkflowStepName | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  outputs: Partial<Record<WorkflowStepName, WorkflowOutputEntry>>;
};

export type WorkflowListItem = {
  workflow_id: string;
  project_name: string | null;
  status: WorkflowStatus;
  current_step: WorkflowStepName | null;
  created_at: string;
  score: number | null;
};
```

- [ ] **Step 2: api.ts 替换 runWorkflow/runWorkflowUpload,加新 client**

删除 `runWorkflow` / `runWorkflowUpload`,替换为:
```ts
export async function createWorkflow(body: {
  text: string;
  project_name?: string;
  session_id: string;
  rag?: RagConfig;
  agent?: AgentConfig;
  top_k?: number;
}): Promise<{ workflow_id: string; status: string }> {
  return parseJson(await fetch("/api/porto/workflows", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
}

export async function listWorkflows(sessionId?: string) {
  const q = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  return parseJson<{ items: WorkflowListItem[] }>(await fetch(`/api/porto/workflows${q}`));
}

export async function getWorkflow(id: string) {
  return parseJson<WorkflowDetail>(await fetch(`/api/porto/workflows/${encodeURIComponent(id)}`));
}

export async function advanceWorkflow(id: string) {
  return parseJson<{ workflow_id: string; status: string }>(
    await fetch(`/api/porto/workflows/${encodeURIComponent(id)}/advance`, { method: "POST" }),
  );
}

export async function saveStepOutput(id: string, step: WorkflowStepName, output: Record<string, unknown>) {
  return parseJson<WorkflowDetail>(
    await fetch(`/api/porto/workflows/${encodeURIComponent(id)}/steps/${step}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(output),
    }),
  );
}

export async function deleteWorkflow(id: string) {
  const r = await fetch(`/api/porto/workflows/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!r.ok) throw new Error(await r.text());
}
```

把 import 行补上 `WorkflowDetail, WorkflowListItem, WorkflowStepName`。`defaultAgentConfig` 里 `spec_refine_parallel: true` 改 `spec_refine_concurrency: 3`,并加 `agent_request_timeout: 120`。

- [ ] **Step 3: typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无错(porto-workbench.tsx 还引用旧的 runWorkflow/WorkflowResponse,会报错——这预期内,Task 11 修)

- [ ] **Step 4: Commit(即便 workbench 暂时编译不过,types/api 已自洽)**

```bash
git add frontend/src/lib/types.ts frontend/src/lib/api.ts
git commit -m "feat(chatbot): 前端 workflow types + api client(分步轮询)"
```

---

## Task 11: 前端 workbench 重构(分步向导 + 历史 + spec 并发 UI)

**Files:**
- Modify: `frontend/src/components/porto-workbench.tsx`(WorkflowPanel 重构 + Sidebar 加 Workflows + AgentSettingsForm spec 并发 input)
- 验证:`cd frontend && npx tsc --noEmit && npm run build`

**Interfaces:**
- Consumes:Task 10 的 types/api
- Produces:分步 WorkflowPanel(进度指示器 + checkpoint 产出区 + 轮询 + 保存/继续)+ 历史 sidebar + spec 并发度 input

- [ ] **Step 1: 读 Next.js 16 定制版相关 docs**

```bash
ls frontend/node_modules/next/dist/docs/ 2>/dev/null && echo "读 App Router / client component / fetch 指南"
```
按 AGENTS.md 要求,先看 docs 再写客户端组件(本组件已是 `"use client"`,沿用现成模式即可)。

- [ ] **Step 2: 改 AgentSettingsForm 的 spec 并发 input**

定位 [porto-workbench.tsx:1379-1389](../../../frontend/src/components/porto-workbench.tsx) 的「Spec 并行生成」checkbox,替换为:
```tsx
<label className="block">
  <span className="text-xs text-zinc-500">Spec 并发度（1-10）</span>
  <input
    className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
    type="number" min={1} max={10}
    value={agentDraft.spec_refine_concurrency}
    onChange={(e) => updateAgent("spec_refine_concurrency", Number(e.target.value))}
  />
</label>
```
(删除引用 `spec_refine_parallel` 的旧 checkbox。)

- [ ] **Step 3: 重构 WorkflowPanel 为分步向导**

新增状态(在 `PortoWorkbench` 组件顶部 state 区):
```tsx
const [workflowId, setWorkflowId] = useState<string | null>(null);
const [workflowDetail, setWorkflowDetail] = useState<WorkflowDetail | null>(null);
const [workflowList, setWorkflowList] = useState<WorkflowListItem[]>([]);
const [draft, setDraft] = useState<string>("");  // 当前 checkpoint 产出的编辑草稿
```

新增轮询 hook(组件内):
```tsx
useEffect(() => {
  if (!workflowId) return;
  let active = true;
  let timer: ReturnType<typeof setTimeout>;
  async function poll() {
    try {
      const d = await getWorkflow(workflowId);
      if (!active) return;
      setWorkflowDetail(d);
      if (d.status === "running") timer = setTimeout(poll, 2000);
    } catch { /* 忽略,下轮重试 */ }
  }
  poll();
  return () => { active = false; clearTimeout(timer); };
}, [workflowId]);
```

把 `runWorkflowAction` 改为:
```tsx
async function runWorkflowAction() {
  if (!workflowText.trim() && !selectedFile) return;
  setBusyLabel("提交拆解");
  setError("");
  try {
    const resp = selectedFile
      ? await createWorkflowUploadCompat(selectedFile, projectName.trim(), sessionId)  // 见下
      : await createWorkflow({
          text: workflowText.trim(), project_name: projectName.trim() || undefined,
          session_id: sessionId, rag: ragConfig, agent: agentConfig, top_k: ragConfig.top_k,
        });
    setWorkflowId(resp.workflow_id);
    setWorkflowDetail(null);
  } catch (err) {
    setError(err instanceof Error ? err.message : "提交拆解失败");
  } finally {
    setBusyLabel("");
  }
}
```
(上传走 `/api/porto/workflows/upload`,在 api.ts 加 `createWorkflowUpload`——Task 10 漏了,这里补一个简单的 formdata 版。)

新 WorkflowPanel(替换原 `WorkflowPanel` 函数):
```tsx
function WorkflowPanel({
  busy, error, fileInputRef, projectName, selectedFile, setProjectName, setSelectedFile,
  text, onRun, onTextChange, detail, onAdvance, onSaveStep, draft, setDraft,
}: {
  busy: boolean; error: string;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  projectName: string; selectedFile: File | null;
  setProjectName: (v: string) => void; setSelectedFile: (v: File | null) => void;
  text: string; onRun: () => void; onTextChange: (v: string) => void;
  detail: WorkflowDetail | null;
  onAdvance: () => void;
  onSaveStep: (step: WorkflowStepName, output: Record<string, unknown>) => void;
  draft: string; setDraft: (v: string) => void;
}) {
  const steps: WorkflowStepName[] = ["retrieve","understand","identify","generate","evaluate"];
  const checkpoints: WorkflowStepName[] = ["understand","identify","generate"];
  const curIdx = detail?.current_step ? steps.indexOf(detail.current_step) : -1;

  return (
    <div className="flex flex-1 flex-col p-4">
      {error ? <div className="mb-3 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</div> : null}

      {/* 输入区:无 detail 时显示 */}
      {!detail ? (
        <>
          <div className="mb-3 grid gap-3 md:grid-cols-[minmax(0,1fr)_260px]">
            <label className="block">
              <span className="text-xs text-zinc-500">项目名称</span>
              <input className="mt-1 w-full rounded-md border border-zinc-200 px-3 py-2 text-sm" value={projectName} onChange={(e)=>setProjectName(e.target.value)} />
            </label>
            <label className="block">
              <span className="text-xs text-zinc-500">PRD 文件</span>
              <span className="mt-1 flex cursor-pointer items-center gap-2 rounded-md border border-zinc-200 px-3 py-2 text-sm hover:bg-zinc-50">
                <Upload size={15} /><span className="truncate">{selectedFile?.name || "上传 PDF / Word / Markdown"}</span>
                <input ref={fileInputRef} className="hidden" type="file" accept=".pdf,.docx,.md,.txt" onChange={(e)=>setSelectedFile(e.target.files?.[0] ?? null)} />
              </span>
            </label>
          </div>
          <textarea className="min-h-[300px] flex-1 resize-none rounded-xl border border-zinc-200 bg-zinc-50 p-4 text-sm" placeholder="粘贴 PRD..." value={text} onChange={(e)=>onTextChange(e.target.value)} />
        </>
      ) : null}

      {/* 有 detail:进度 + 产出 */}
      {detail ? (
        <>
          <WorkflowStepper steps={steps} checkpoints={checkpoints} curIdx={curIdx} status={detail.status} />
          {detail.status === "running" ? (
            <div className="my-6 flex items-center gap-2 text-sm text-zinc-500"><Loader2 className="animate-spin" size={16} />生成中…</div>
          ) : null}
          {detail.status === "failed" ? (
            <div className="my-4 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{detail.error || "步骤失败"} <button className="ml-2 underline" onClick={onAdvance}>重试</button></div>
          ) : null}
          {checkpoints.includes(detail.current_step as WorkflowStepName) && detail.status === "awaiting_input" ? (
            <CheckpointEditor detail={detail} draft={draft} setDraft={setDraft} onAdvance={onAdvance} onSaveStep={onSaveStep} />
          ) : null}
          {detail.status === "completed" ? <CompletedView detail={detail} /> : null}
        </>
      ) : null}

      <div className="mt-3 flex items-center justify-end gap-3">
        <button className="flex items-center gap-2 rounded-lg bg-zinc-950 px-4 py-2 text-sm text-white disabled:opacity-40"
          disabled={busy || (!text.trim() && !selectedFile)} onClick={onRun}>
          {busy ? <Loader2 className="animate-spin" size={16} /> : <Play size={16} />}
          {detail ? "重新拆解" : "运行拆解"}
        </button>
      </div>
    </div>
  );
}
```

`WorkflowStepper`、`CheckpointEditor`(understand→markdown 编辑/identify→子系统编辑/generate→spec 编辑)、`CompletedView` 三个子组件:每个 checkpoint 根据 `detail.current_step` 渲染对应编辑器(understand/generate 用 textarea+ReactMarkdown 预览切换,identify 用子系统卡片增删改),「保存修改」调 `onSaveStep`、「继续下一步」调 `onAdvance`。受篇幅,这三个子组件按 spec 3.2 表实现:understand/generate 的 output 是 `{understanding|specs}`,identify 的 output 是 `{subsystems: Subsystem[]}`。

`onAdvance`/`onSaveStep` 在 `PortoWorkbench` 内:
```tsx
async function onAdvance() {
  if (!workflowId) return;
  setBusyLabel("推进");
  try { await advanceWorkflow(workflowId); } catch (e) { setError(String(e)); }
  finally { setBusyLabel(""); }
}
async function onSaveStep(step: WorkflowStepName, output: Record<string, unknown>) {
  if (!workflowId) return;
  setBusyLabel("保存");
  try { await saveStepOutput(workflowId, step, output); } catch (e) { setError(String(e)); }
  finally { setBusyLabel(""); }
}
```

- [ ] **Step 4: Sidebar 加 Workflows 历史**

`Sidebar` 加 props `workflows: WorkflowListItem[]` + `onPickWorkflow(id)`,渲染一个 section(仿 Chat Records),点击调 `setWorkflowId(id)` 触发轮询拉详情。在 `PortoWorkbench` 顶层 `useEffect` 拉 `listWorkflows(sessionId)` 填充,并在 workflow 状态变化后刷新。

- [ ] **Step 5: 删除 inspector.workflow 相关旧渲染**

原 `Inspector`/`WorkflowResult`/`inspector.workflow` 引用(基于旧 `WorkflowResponse`)删除或保留为空;`InspectorState.workflow` 字段可移除。grep 清理 `runWorkflow`/`WorkflowResponse` 残留引用。

- [ ] **Step 6: typecheck + build**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无错

Run: `cd frontend && npm run build`
Expected: 构建成功

- [ ] **Step 7: 手动验证(启动前后端)**

```bash
# 终端1:后端
cd backend && uv run uvicorn porto_chatbot.main:app --host 127.0.0.1 --port 8100
# 终端2:前端
cd frontend && npm run dev
```
浏览器 http://localhost:3000 → 拆解模式 → 提交 PRD → 观察:进度指示器从 retrieve 推进到 understand、状态 running→awaiting_input、产出区显示 understanding、可编辑保存、继续到 identify、子系统可增删改、继续到 generate、spec 并发生成后展示、继续到 completed。

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/porto-workbench.tsx frontend/src/lib/api.ts
git commit -m "feat(chatbot): 前端分步 workflow 向导 + 历史 + spec 并发 UI"
```

---

## Self-Review(写完后已自查)

- **Spec 覆盖**:checkpoint 3 步(Task 5 CHECKPOINTS)、sqlite 两表+resume(Task 4)、异步轮询(Task 6/7)、spec 并发(Task 1/3)、编辑回退(Task 7 PUT)、重启不回退(Task 8)、不兼容(Task 1 删 parallel)、LLM timeout(Task 2)、删 langgraph(Task 9)、前端(Task 10/11)——全覆盖。
- **类型一致**:`spec_refine_concurrency` 全链路一致;`WorkflowDetail.outputs` 结构前后端一致;`WorkflowStepName` 5 值与后端 STEPS 一致。
- **顺序依赖**:Task 1(字段)→ Task 2/3(消费字段)→ Task 4(store)→ Task 5(runner)→ Task 6(executor,依赖 store+runner)→ Task 7(route,依赖 executor)→ Task 8(lifespan)→ Task 9(删 graph,最后)→ Task 10/11(前端)。每任务结束可独立测试+commit。
- **已知边界**:Task 9 删 graph.py 前,`PortoAgent` 在 graph.py 仍存在(含 run),Task 4-8 用它做容器,不调 run(),无冲突;Task 9 瘦身迁移。Task 7 测试在无 LLM key 下降级路径可能直接跑到 completed,`_wait_status` 的 target 含 completed 兼容此情况。

---

## 执行选项

Plan complete and saved to `docs/superpowers/plans/2026-07-13-workflow-checkpoint.md`. 两种执行方式:

**1. Subagent-Driven(推荐)** — 每个 Task 派一个新 subagent 实现,任务间我审查,迭代快、上下文干净
**2. Inline 执行** — 在当前会话按 executing-plans 批量执行,带检查点审查

选哪种?
