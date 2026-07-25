# Tool-calling 截断治理 + 整步重跑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 tool-calling 截断后产出残缺过渡语的 bug，让截断产出固定提示、元数据落库、支持手动整步重跑（turn ×1.5，隐藏硬上限 40）。

**Architecture:** `LLMClient.complete_with_tools` 截断时清空 text + 置 truncated（B 方案，不做收尾 invoke）；节点层产出固定提示，把 `tool_meta` 塞进 `AgentStep.data`（单步）和 `SpecResult.tool_meta`（generate per-subsystem）；executor 投影附进 `workflow_outputs.output`；新增 `POST /steps/{step}/rerun` 端点绕过 graph 直接调节点函数重跑。

**Tech Stack:** Python 3.14 + FastAPI + langgraph + pydantic（pytest 测试）；Next.js + React + TS（`npm run build` 验证）。

**Spec:** [docs/superpowers/specs/2026-07-25-tool-loop-truncation-design.md](../specs/2026-07-25-tool-loop-truncation-design.md)

## Global Constraints

- 后端测试一律 `cd backend && uv run pytest <path> -v`；构造 Settings 用 `Settings(data_dir=tmp_path, log_dir=tmp_path/"logs")` 避 .env。
- `complete_with_tools` 全仓只有两个调用点：`specs/steps.py:16`（generate_initial_spec）、`agent/nodes/understand.py:14`（understand_prd）。retrieve/identify/evaluate 不调它，本次不动。
- `ToolLoopResult`（`llm/types.py`）已有 `text/tool_calls/turns/truncated`，不改。
- 前端无单测框架；验证用 `cd frontend && npm run build`（tsc/eslint 有假阳性，以 build 为准）。
- 注释与文案默认中文；commit message 中文，结尾 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- TDD：每个后端任务先写失败测试再实现。

## File Structure

| 文件 | 责任 | 任务 |
|------|------|------|
| `backend/src/porto_chatbot/llm/client.py` | 截断分支：text="" + truncated | T1 |
| `backend/src/porto_chatbot/settings.py` | `agent_max_tool_turns` 默认 10；新增 `tool_turn_hard_cap=40` | T2 |
| `backend/src/porto_chatbot/models/spec.py` | `SpecResult` 新增 `tool_meta` 字段 | T3 |
| `backend/src/porto_chatbot/specs/steps.py` | `generate_initial_spec` 返回 `(spec, tool_meta)` | T4 |
| `backend/src/porto_chatbot/specs/loop.py` | 截断时跳 critique/refine，写 `SpecResult.tool_meta` | T4 |
| `backend/src/porto_chatbot/agent/nodes/understand.py` | 固定提示 + `_step.data["tool_meta"]` | T5 |
| `backend/src/porto_chatbot/workflow_store.py` | 新增 `update_agent_snapshot` | T7 |
| `backend/src/porto_chatbot/workflow_executor.py` | 投影 `tool_meta`；新增 `rerun_step` | T6, T7 |
| `backend/src/porto_chatbot/api/routes/workflow.py` | 新增 `POST .../steps/{step}/rerun` | T8 |
| `frontend/src/lib/api.ts` | 默认 4→10；`rerunStep` | T9 |
| `frontend/src/lib/types.ts` | `tool_meta` 类型 | T9 |
| `frontend/src/components/porto-workbench.tsx` | 卡片 B 标记 + 重跑按钮三态 | T10 |

---

### Task 1: `LLMClient.complete_with_tools` 截断分支（B 方案）

**Files:**
- Modify: `backend/src/porto_chatbot/llm/client.py:241-259`
- Test: `backend/tests/test_llm_client_truncation.py`（新建）

**Interfaces:**
- Produces: `complete_with_tools(...)` 截断时返回 `ToolLoopResult(text="", truncated=True, turns=N, tool_calls=[...])`；正常收敛返回 `truncated=False, text=<最终回答>`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_llm_client_truncation.py
from __future__ import annotations
from unittest.mock import MagicMock
from porto_chatbot.llm.client import LLMClient
from porto_chatbot.llm.types import ToolDef


def _client(mock_chat, max_turns=3):
    c = LLMClient.__new__(LLMClient)
    c._client = mock_chat
    c._native_client = None
    c.logger = MagicMock()
    c.settings = MagicMock(agent_max_tool_turns=max_turns, agent_request_timeout=10)
    return c


def _resp(tool_calls=None, content=""):
    r = MagicMock()
    r.tool_calls = tool_calls or []
    r.content = content
    return r


def test_truncation_clears_text_and_marks_truncated():
    chat = MagicMock()
    chat.bind_tools.return_value = chat
    chat.invoke.side_effect = [
        _resp([{"name": "search", "args": {}, "id": "1"}], "我再查一下1"),
        _resp([{"name": "search", "args": {}, "id": "2"}], "我再查一下2"),
        _resp([{"name": "search", "args": {}, "id": "3"}], "我再查一下3"),
    ]
    c = _client(chat, max_turns=3)
    result = c.complete_with_tools("sys", "user", [ToolDef("search", "d", {}, lambda a: "结果")])
    assert result.truncated is True
    assert result.text == ""
    assert result.turns == 3
    assert len(result.tool_calls) == 3


def test_normal_convergence_returns_final_text():
    chat = MagicMock()
    chat.bind_tools.return_value = chat
    chat.invoke.side_effect = [
        _resp([{"name": "search", "args": {}, "id": "1"}], "查一下"),
        _resp([], "最终报告正文"),
    ]
    c = _client(chat, max_turns=4)
    result = c.complete_with_tools("sys", "user", [ToolDef("search", "d", {}, lambda a: "结果")])
    assert result.truncated is False
    assert result.text == "最终报告正文"
    assert result.turns == 2
    assert len(result.tool_calls) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_llm_client_truncation.py -v`
Expected: FAIL（`test_truncation...` 断言 `text==""` 失败——现状返回过渡语；`test_normal_convergence` 应已通过）

- [ ] **Step 3: 替换截断分支**

把 `client.py:241-259`（从 `result.truncated = True` 到 `return result` 的整段收尾逻辑）替换为：

```python
        result.truncated = True
        result.text = ""  # 截断 = 无可靠产出;过渡语清空,不暴露给前端
        self.logger.warning(
            "llm tool loop truncated max_turns=%s total=%s",
            resolved_turns, len(result.tool_calls),
        )
        return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_llm_client_truncation.py -v`
Expected: PASS（2 个测试）

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/llm/client.py backend/tests/test_llm_client_truncation.py
git commit -m "fix(llm): 截断时清空 text + 置 truncated,不暴露过渡语

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: settings 默认值 + 隐藏硬上限

**Files:**
- Modify: `backend/src/porto_chatbot/settings.py:75-78`（`agent_max_tool_turns`）+ 紧随其后新增 `tool_turn_hard_cap`
- Test: `backend/tests/test_settings_truncation.py`（新建）

**Interfaces:**
- Produces: `Settings.agent_max_tool_turns` 默认 10（`le=20` 不变）；`Settings.tool_turn_hard_cap` 默认 40（`ge=1`）。后者不进 `AgentSettingsPayload`（T2 不动 deps.py，仅确认不映射）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_settings_truncation.py
from __future__ import annotations
from porto_chatbot.settings import Settings


def test_default_agent_max_tool_turns_is_10(tmp_path):
    s = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs")
    assert s.agent_max_tool_turns == 10


def test_tool_turn_hard_cap_default_40(tmp_path):
    s = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs")
    assert s.tool_turn_hard_cap == 40
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_settings_truncation.py -v`
Expected: FAIL（`agent_max_tool_turns == 4`；`tool_turn_hard_cap` 属性不存在）

- [ ] **Step 3: 改 settings.py**

`settings.py:75-78` 把

```python
    # --- 节点内 tool calling（Phase 0/1）---
    agent_max_tool_turns: int = Field(default=4, ge=1, le=20)
```

改为

```python
    # --- 节点内 tool calling（Phase 0/1）---
    agent_max_tool_turns: int = Field(default=10, ge=1, le=20)

    # --- 整步重跑 turn 上调的隐藏天花板(不暴露给前端,仅 rerun ×1.5 时 cap)---
    tool_turn_hard_cap: int = Field(default=40, ge=1)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_settings_truncation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/settings.py backend/tests/test_settings_truncation.py
git commit -m "feat(settings): agent_max_tool_turns 默认 4→10; 新增 tool_turn_hard_cap=40

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: `SpecResult` 新增 `tool_meta` 字段

**Files:**
- Modify: `backend/src/porto_chatbot/models/spec.py:30-37`
- Test: `backend/tests/test_spec_result_tool_meta.py`（新建）

**Interfaces:**
- Produces: `SpecResult.tool_meta: dict`（默认 `{}`）。现有 `truncated`（refine-loop 语义）不动。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_spec_result_tool_meta.py
from __future__ import annotations
from porto_chatbot.models import SpecResult


def test_tool_meta_defaults_empty():
    r = SpecResult(final="x")
    assert r.tool_meta == {}


def test_tool_meta_carries_truncation_info():
    r = SpecResult(final="x", tool_meta={"turns": 4, "truncated": True})
    assert r.tool_meta["truncated"] is True
    assert r.model_dump()["tool_meta"]["turns"] == 4
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_spec_result_tool_meta.py -v`
Expected: FAIL（`tool_meta` 字段不存在 → TypeError）

- [ ] **Step 3: 改 models/spec.py**

`SpecResult` 加一行字段：

```python
class SpecResult(BaseModel):
    """单个子系统 spec 的 loop 产物。"""

    final: str
    attempts: list[SpecAttempt] = Field(default_factory=list)
    iterations: int = 0
    truncated: bool = False          # refine-loop 截断(max_iter/budget),语义不动
    used_llm: bool = False
    tool_meta: dict = Field(default_factory=dict)  # tool-calling 元数据;truncated 键 = tool 截断
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_spec_result_tool_meta.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/models/spec.py backend/tests/test_spec_result_tool_meta.py
git commit -m "feat(models): SpecResult 加 tool_meta 字段(tool-calling 元数据)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: `generate_initial_spec` 返回 tool 元数据 + loop 消费

**Files:**
- Modify: `backend/src/porto_chatbot/specs/steps.py:11-25`（`generate_initial_spec`）
- Modify: `backend/src/porto_chatbot/specs/loop.py:21-23, 72-78`（消费 + 两条 return 路径）
- Test: `backend/tests/test_spec_loop_tool_truncation.py`（新建）

**Interfaces:**
- Consumes: T1（`ToolLoopResult.truncated`）、T3（`SpecResult.tool_meta`）
- Produces: `generate_initial_spec(ctx, sub) -> tuple[str, dict]`；`generate_spec_with_loop` 在 tool 截断时跳 critique/refine，`SpecResult.tool_meta` 填充。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_spec_loop_tool_truncation.py
from __future__ import annotations
from unittest.mock import MagicMock
from porto_chatbot.models import Subsystem
from porto_chatbot.specs.loop import generate_spec_with_loop
import porto_chatbot.specs.loop as loop_mod


def test_loop_skips_critique_when_tool_truncated(monkeypatch):
    tool_meta = {"turns": 4, "tool_calls": 11, "truncated": True,
                 "max_turns": 10, "reason": "tool_loop_truncated"}
    monkeypatch.setattr(loop_mod, "generate_initial_spec",
                        lambda ctx, sub: ("⚠️ 规格生成超限", tool_meta))
    ctx = MagicMock()
    ctx.llm.enabled = True
    ctx.settings.spec_refine_enabled = True
    ctx.settings.spec_refine_max_iter = 3
    ctx.settings.spec_refine_pass_score = 10
    ctx.settings.spec_refine_budget_tokens = 40000
    result = generate_spec_with_loop(ctx, Subsystem(name="x", responsibility="r"))
    assert result.tool_meta["truncated"] is True
    assert "超限" in result.final
    ctx.critic_llm.complete_structured.assert_not_called()  # critique 被跳过


def test_loop_normal_carries_tool_meta(monkeypatch):
    tool_meta = {"turns": 2, "tool_calls": 1, "truncated": False,
                 "max_turns": 10, "reason": None}
    monkeypatch.setattr(loop_mod, "generate_initial_spec",
                        lambda ctx, sub: ("正常 spec", tool_meta))
    ctx = MagicMock()
    ctx.llm.enabled = True
    ctx.critic_llm.enabled = False  # critic 不可用 → 立即接受
    ctx.settings.spec_refine_enabled = True
    ctx.settings.spec_refine_max_iter = 3
    ctx.settings.spec_refine_pass_score = 10
    ctx.settings.spec_refine_budget_tokens = 40000
    result = generate_spec_with_loop(ctx, Subsystem(name="x", responsibility="r"))
    assert result.tool_meta["truncated"] is False
    assert result.tool_meta["turns"] == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_spec_loop_tool_truncation.py -v`
Expected: FAIL（`generate_initial_spec` 现返回 str，解构失败）

- [ ] **Step 3a: 改 `specs/steps.py` 的 `generate_initial_spec`**

替换 `steps.py:11-25`：

```python
_TRUNCATED_NOTICE_SPEC = (
    "⚠️ 规格生成未能完成：本子系统工具调用已达上限（{calls}/{limit} turn）。建议重跑本步。"
)


def generate_initial_spec(ctx: SpecContext, sub: Subsystem) -> tuple[str, dict]:
    """LLM 生成首版 spec(带工具)。返回 (spec_text, tool_meta)。

    tool 截断时 spec_text = 固定提示,由 loop 层跳过 critique/refine。
    LLM 未启用时返回 ("", 空 tool_meta)。
    """
    max_turns = ctx.settings.agent_max_tool_turns
    if not ctx.llm.enabled:
        return "", {"turns": 0, "tool_calls": 0, "truncated": False,
                    "max_turns": max_turns, "reason": None}
    tools_ctx = AgentToolContext(state=ctx.state, vector_store=ctx.vector_store)
    result = ctx.llm.complete_with_tools(
        f"你是资深系统规格工程师。为子系统 {sub.name} 生成详细的系统需求规格（markdown）。"
        f"子系统职责：{sub.responsibility}；能力：{', '.join(sub.capabilities) or '（待识别）'}。"
        f"必须包含这些章节：{', '.join(_SPEC_SECTIONS)}。"
        "API 需求要给出具体端点/方法/输入输出/错误码；数据模型要列实体与关键字段；验收标准要具体可测。"
        "可调用工具检索知识库以参考现有系统约定。",
        f"请生成 {sub.name} 的规格文档。",
        build_agent_tools(tools_ctx),
    )
    tool_meta = {
        "turns": result.turns,
        "tool_calls": len(result.tool_calls),
        "truncated": result.truncated,
        "max_turns": max_turns,
        "reason": "tool_loop_truncated" if result.truncated else None,
    }
    if result.truncated:
        return _TRUNCATED_NOTICE_SPEC.format(
            calls=tool_meta["tool_calls"], limit=max_turns), tool_meta
    return (result.text or "").strip(), tool_meta
```

- [ ] **Step 3b: 改 `specs/loop.py`**

`loop.py:21-23` 把

```python
    spec = generate_initial_spec(ctx, sub)
    if not spec:
        spec = render_template_spec(ctx, sub)  # 生成失败降级模板
```

改为

```python
    spec, tool_meta = generate_initial_spec(ctx, sub)
    # tool-calling 截断:spec 已是固定提示,跳过 critique/refine
    if tool_meta.get("truncated"):
        return SpecResult(
            final=spec, attempts=[], iterations=0,
            truncated=False, used_llm=True, tool_meta=tool_meta,
        )
    if not spec:
        spec = render_template_spec(ctx, sub)  # 生成失败降级模板
```

`loop.py:72-78` 的最终 return 把 `tool_meta=tool_meta` 加上：

```python
    return SpecResult(
        final=best,
        attempts=attempts,
        iterations=len(attempts),
        truncated=truncated,
        used_llm=True,
        tool_meta=tool_meta,
    )
```

另外 `loop.py:15` 的 LLM 未启用分支也补 `tool_meta={}`（该分支 `SpecResult(final=..., used_llm=False)` 加 `tool_meta={}`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_spec_loop_tool_truncation.py -v`
Expected: PASS（2 个）

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/specs/steps.py backend/src/porto_chatbot/specs/loop.py backend/tests/test_spec_loop_tool_truncation.py
git commit -m "feat(spec): generate_initial_spec 返回 tool_meta; loop 截断时跳 critique/refine

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: understand 节点固定提示 + `_step.data["tool_meta"]`

**Files:**
- Modify: `backend/src/porto_chatbot/agent/nodes/understand.py:11-41`
- Test: `backend/tests/test_understand_node_truncation.py`（新建）

**Interfaces:**
- Consumes: T1（`ToolLoopResult.truncated`）
- Produces: understand 截断时 `understanding` = 固定提示；返回的 `steps[0].data["tool_meta"]` 含 turns/tool_calls/truncated/max_turns/reason。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_understand_node_truncation.py
from __future__ import annotations
from unittest.mock import MagicMock
from porto_chatbot.agent.nodes.understand import understand_prd
from porto_chatbot.llm.types import ToolLoopResult
from porto_chatbot.models import AgentStep


def _agent(truncated, turns=4, n_calls=11, max_turns=10):
    agent = MagicMock()
    agent.llm.enabled = True
    agent.llm.complete_with_tools.return_value = ToolLoopResult(
        text="", tool_calls=[object()] * n_calls, turns=turns, truncated=truncated)
    agent.settings.agent_max_tool_turns = max_turns
    # 用真实 _step(只记 data)
    agent._step = lambda name, summary, data: {"steps": [AgentStep(
        name=name, status="completed", summary=summary, data=data)]}
    return agent


def test_understand_truncated_uses_notice_and_meta():
    agent = _agent(truncated=True)
    state = {"workflow_id": "w1", "prd_text": "xxx", "sources": []}
    out = understand_prd(state, config={"configurable": {"agent": agent}})
    assert "未能完成" in out["understanding"]
    tm = out["steps"][0].data["tool_meta"]
    assert tm["truncated"] is True
    assert tm["turns"] == 4
    assert tm["tool_calls"] == 11
    assert tm["max_turns"] == 10


def test_understand_normal_keeps_text():
    agent = MagicMock()
    agent.llm.enabled = True
    agent.llm.complete_with_tools.return_value = ToolLoopResult(
        text="正常理解报告", tool_calls=[object()], turns=2, truncated=False)
    agent.settings.agent_max_tool_turns = 10
    agent._step = lambda name, summary, data: {"steps": [AgentStep(
        name=name, status="completed", summary=summary, data=data)]}
    state = {"workflow_id": "w1", "prd_text": "xxx", "sources": []}
    out = understand_prd(state, config={"configurable": {"agent": agent}})
    assert out["understanding"] == "正常理解报告"
    assert out["steps"][0].data["tool_meta"]["truncated"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_understand_node_truncation.py -v`
Expected: FAIL（截断时 `understanding` 仍是过渡语/空，无 tool_meta）

- [ ] **Step 3: 改 understand.py**

替换 `understand.py:11-41`（从 `understanding = ""` 到 return 结束）：

```python
_TRUNCATED_NOTICE = (
    "⚠️ 业务理解未能完成：本步工具调用已达上限（{calls}/{limit} turn）。建议重跑本步。"
)


def understand_prd(state, *, config):
    agent = config["configurable"]["agent"]
    agent.logger.info("step understand_prd start workflow_id=%s", state.get("workflow_id"))
    max_turns = agent.settings.agent_max_tool_turns
    understanding = ""
    tool_meta = {"turns": 0, "tool_calls": 0, "truncated": False,
                 "max_turns": max_turns, "reason": None}
    if agent.llm.enabled:
        ctx = AgentToolContext(state=state, vector_store=agent.vector_store)
        result = agent.llm.complete_with_tools(
            "你是资深业务分析师。根据 PRD 和知识库片段，输出简洁的中文业务理解报告，"
            "包含：执行摘要、业务目标、核心实体、子系统线索。"
            "可调用工具获取 PRD 原文与检索知识库，自主决定检索什么。",
            "请生成业务理解报告。",
            build_agent_tools(ctx),
        )
        tool_meta = {
            "turns": result.turns,
            "tool_calls": len(result.tool_calls),
            "truncated": result.truncated,
            "max_turns": max_turns,
            "reason": "tool_loop_truncated" if result.truncated else None,
        }
        if result.truncated:
            understanding = _TRUNCATED_NOTICE.format(
                calls=tool_meta["tool_calls"], limit=max_turns)
            agent.logger.info(
                "step understand_prd truncated workflow_id=%s turns=%s calls=%s",
                state.get("workflow_id"), result.turns, len(result.tool_calls))
        else:
            understanding = (result.text or "").strip()
            agent.logger.info(
                "step understand_prd llm tool_calls=%s turns=%s chars=%s",
                len(result.tool_calls), result.turns, len(understanding))
    if not understanding:
        understanding = _fallback_understanding(state)
        agent.logger.info(
            "step understand_prd used fallback workflow_id=%s", state.get("workflow_id"))
    return {
        "understanding": understanding,
        "current_step": "understand",
        **agent._step(
            "understand_prd",
            "完成业务理解报告",
            {"chars": len(understanding), "used_llm": bool(understanding) and agent.llm.enabled,
             "tool_meta": tool_meta},
        ),
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_understand_node_truncation.py -v`
Expected: PASS（2 个）

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/agent/nodes/understand.py backend/tests/test_understand_node_truncation.py
git commit -m "feat(understand): 截断时产出固定提示 + tool_meta 进 _step.data

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: executor 投影 `tool_meta` 到 `workflow_outputs`

**Files:**
- Modify: `backend/src/porto_chatbot/workflow_executor.py:230-267`（`_project_state`）
- Test: `backend/tests/test_workflow_executor.py`（追加）

**Interfaces:**
- Consumes: T3（`SpecResult.tool_meta`）、T5（`AgentStep.data["tool_meta"]`）
- Produces: `workflow_outputs[step].output["tool_meta"]` —— 单步为扁平 dict；generate 为 `{"truncated": bool}` 汇总（per-subsystem 已在 `spec_results` 自带）。

- [ ] **Step 1: 写失败测试（追加到 test_workflow_executor.py）**

```python
def test_project_state_attaches_tool_meta_single_step(tmp_path):
    from porto_chatbot.models import AgentStep
    from unittest.mock import MagicMock
    store = WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))
    wid = store.create("s", "p", "prd", 6, {}, {"agent_max_tool_turns": 10})
    executor = WorkflowExecutor(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"), store, graph=MagicMock())
    snap = MagicMock()
    snap.values = {
        "understanding": "报告",
        "steps": [AgentStep(name="understand_prd", status="completed",
                            data={"tool_meta": {"turns": 4, "truncated": True, "max_turns": 10}})],
        "spec_results": {},
    }
    snap.next = ("identify",)
    executor.graph.get_state = MagicMock(return_value=snap)
    executor._project_state(wid, {"configurable": {"thread_id": wid}})
    out = store.get_outputs(wid)["understand"]["output"]
    assert out["understanding"] == "报告"
    assert out["tool_meta"]["truncated"] is True
    assert out["tool_meta"]["turns"] == 4


def test_project_state_attaches_tool_meta_generate(tmp_path):
    from unittest.mock import MagicMock
    store = WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))
    wid = store.create("s", "p", "prd", 6, {}, {"agent_max_tool_turns": 10})
    executor = WorkflowExecutor(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"), store, graph=MagicMock())
    snap = MagicMock()
    snap.values = {
        "specs": {"wallet": "s1", "proc": "s2"},
        "spec_results": {
            "wallet": {"final": "s1", "tool_meta": {"truncated": True}},
            "proc": {"final": "s2", "tool_meta": {"truncated": False}},
        },
        "steps": [],
    }
    snap.next = ("evaluate",)
    executor.graph.get_state = MagicMock(return_value=snap)
    executor._project_state(wid, {"configurable": {"thread_id": wid}})
    out = store.get_outputs(wid)["generate"]["output"]
    assert out["tool_meta"]["truncated"] is True  # any
```

> 注：`spec_results` 在 graph state 里是 `SpecResult` 对象，投影经 `_to_jsonable` 转 dict；测试直接给 dict 模拟投影后的形态。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_executor.py::test_project_state_attaches_tool_meta_single_step tests/test_workflow_executor.py::test_project_state_attaches_tool_meta_generate -v`
Expected: FAIL（`output` 无 `tool_meta` 键）

- [ ] **Step 3: 改 `_project_state`**

在 `workflow_executor.py:230-267` 的 `_project_state` 内，`for step in completed:` 循环里写完 `save_output` 后，**额外附加 `tool_meta`**。把当前的：

```python
        for step in completed:
            out = {
                k: _to_jsonable(values[k]) for k in _STEP_OUTPUT_KEYS.get(step, []) if k in values
            }
            if not out:
                continue
            existing_step = existing.get(step)
            forced = step in overrides
            if not forced and existing_step and existing_step["output"] == out:
                continue
            produced_by = overrides.get(step) or (existing_step or {}).get(
                "produced_by", default_produced_by
            )
            self.store.save_output(workflow_id, step, out, produced_by)
```

改为（在 `save_output` 前把 `tool_meta` 塞进 `out`）：

```python
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
                continue
            produced_by = overrides.get(step) or (existing_step or {}).get(
                "produced_by", default_produced_by
            )
            self.store.save_output(workflow_id, step, out, produced_by)
```

并在 `_project_state` 下方新增 helper：

```python
    @staticmethod
    def _tool_meta_for(step: str, values: dict) -> dict | None:
        """单步:从 state.steps 找该步 AgentStep.data["tool_meta"]。
        generate:spec_results 已自带 per-subsystem tool_meta(经 _to_jsonable 转 dict),
        此处只返回 step 级聚合 {truncated: any} 供红 chip 判断。
        """
        if step == "generate":
            spec_results = values.get("spec_results") or {}
            per = {name: (_to_jsonable(r).get("tool_meta") or {}) for name, r in spec_results.items()}
            truncated = any(tm.get("truncated") for tm in per.values())
            return {"truncated": truncated}
        steps = values.get("steps") or []
        for st in reversed(steps):  # 取该 step 最新一条
            data = getattr(st, "data", None) or (st.get("data") if isinstance(st, dict) else None)
            if data and "tool_meta" in data:
                return data["tool_meta"]
        return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_executor.py -v`
Expected: PASS（含原有 + 2 个新测试）

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/workflow_executor.py backend/tests/test_workflow_executor.py
git commit -m "feat(executor): 投影 tool_meta 进 workflow_outputs(单步扁平/generate 聚合)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: `workflow_store.update_agent_snapshot` + `executor.rerun_step`

**Files:**
- Modify: `backend/src/porto_chatbot/workflow_store.py`（新增 `update_agent_snapshot`）
- Modify: `backend/src/porto_chatbot/workflow_executor.py`（新增 `rerun_step` + `_run_rerun`）
- Test: `backend/tests/test_workflow_executor.py`（追加 rerun 测试）、`backend/tests/test_workflow_store.py`（追加 update_agent_snapshot 测试）

**Interfaces:**
- Consumes: T2（`tool_turn_hard_cap`）
- Produces: `WorkflowStore.update_agent_snapshot(workflow_id, updates: dict)`；`WorkflowExecutor.rerun_step(workflow_id, step)` —— 计算 new_max、持久化、绕过 graph 调节点函数重跑。`_NODE_FNS` 从 `agent.graph` 复用（已存在，模块级）。

- [ ] **Step 1: 写失败测试（store）**

追加到 `backend/tests/test_workflow_store.py`：

```python
def test_update_agent_snapshot_merges(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "proj", "prd", 6, {"r": 1}, {"agent_max_tool_turns": 10, "other": 1})
    ok = s.update_agent_snapshot(wid, {"agent_max_tool_turns": 15})
    assert ok is True
    import json
    snap = json.loads(s.get(wid)["agent_snapshot"])
    assert snap["agent_max_tool_turns"] == 15
    assert snap["other"] == 1  # 未给的键保留


def test_update_agent_snapshot_missing_returns_false(tmp_path):
    s = _store(tmp_path)
    assert s.update_agent_snapshot("nope", {"x": 1}) is False
```

写失败测试（executor rerun）追加到 `test_workflow_executor.py`：

```python
def test_rerun_step_scales_max_turns_and_persists(tmp_path):
    store = WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))
    wid = store.create("s", "p", "prd", 6, {}, {"agent_max_tool_turns": 10})
    # trivial graph + 真 agent 重建受 snapshot 驱动;此处直接验证 new_max 算法 + 持久化
    import json
    from unittest.mock import MagicMock
    executor = WorkflowExecutor(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"), store, graph=MagicMock())
    new_max = executor._next_max_turns(wid)  # ceil(10*1.5)=15
    assert new_max == 15
    executor._apply_new_max(wid, new_max)
    snap = json.loads(store.get(wid)["agent_snapshot"])
    assert snap["agent_max_tool_turns"] == 15


def test_rerun_step_caps_at_hard_cap(tmp_path):
    store = WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))
    wid = store.create("s", "p", "prd", 6, {}, {"agent_max_tool_turns": 30})
    executor = WorkflowExecutor(
        Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", tool_turn_hard_cap=40), store, graph=MagicMock())
    assert executor._next_max_turns(wid) == 40  # ceil(30*1.5)=45 → cap 40


def test_rerun_step_at_cap_raises(tmp_path):
    store = WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))
    wid = store.create("s", "p", "prd", 6, {}, {"agent_max_tool_turns": 40})
    executor = WorkflowExecutor(
        Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", tool_turn_hard_cap=40), store, graph=MagicMock())
    import pytest
    with pytest.raises(WorkflowRunning):
        executor.rerun_step(wid, "understand")
```

> 注：`_next_max_turns` / `_apply_new_max` 拆成可单测的纯函数/小方法，便于不启动后台线程就验证算术。撞顶复用 `WorkflowRunning` 异常（路由层统一 409）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_store.py::test_update_agent_snapshot_merges tests/test_workflow_store.py::test_update_agent_snapshot_missing_returns_false tests/test_workflow_executor.py::test_rerun_step_scales_max_turns_and_persists tests/test_workflow_executor.py::test_rerun_step_caps_at_hard_cap tests/test_workflow_executor.py::test_rerun_step_at_cap_raises -v`
Expected: FAIL（方法不存在）

- [ ] **Step 3a: 改 `workflow_store.py`**

在 `update_status` 后新增：

```python
    def update_agent_snapshot(self, workflow_id, updates: dict) -> bool:
        """合并更新 workflows.agent_snapshot(JSON)里的键。返回 False = workflow 不存在。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT agent_snapshot FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if row is None:
                return False
            snap = json.loads(row["agent_snapshot"])
            snap.update(updates)
            conn.execute(
                "UPDATE workflows SET agent_snapshot=?, updated_at=? WHERE workflow_id=?",
                (json.dumps(snap, ensure_ascii=False),
                 datetime.now(UTC).isoformat(), workflow_id),
            )
        return True
```

- [ ] **Step 3b: 改 `workflow_executor.py`**

顶部 import 区加：

```python
import math
```

在 `WorkflowRunning` 异常类定义后或 `update_step` 附近，新增 rerun 相关方法：

```python
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
        threading.Thread(
            target=self._worker_rerun, args=(workflow_id, guard, step), daemon=True
        ).start()

    def _worker_rerun(self, workflow_id: str, guard: threading.Lock, step: str) -> None:
        try:
            self._run_rerun(workflow_id, step)
        except Exception:
            logger.exception("workflow rerun crashed workflow_id=%s", workflow_id)
            try:
                self.store.update_status(workflow_id, "failed", error="rerun crashed")
            except Exception:
                pass
        finally:
            guard.release()

    def _run_rerun(self, workflow_id: str, step: str) -> None:
        from .agent.graph import _NODE_FNS

        row = self.store.get(workflow_id)
        new_max = self._next_max_turns(workflow_id)
        self._apply_new_max(workflow_id, new_max)
        self.store.update_status(workflow_id, "running")
        agent = self._build_agent(self.store.get(workflow_id))  # 含 new_max 的 snapshot
        config = self._config(workflow_id, agent)
        state = self.graph.get_state(config).values
        node_fn = _NODE_FNS[step]
        partial = node_fn(state, config=config)
        self.graph.update_state(config, partial, as_node=step)
        self._project_state(workflow_id, config, default_produced_by="ai")
        self._sync_status(workflow_id, config)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_store.py tests/test_workflow_executor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/workflow_store.py backend/src/porto_chatbot/workflow_executor.py backend/tests/test_workflow_store.py backend/tests/test_workflow_executor.py
git commit -m "feat(rerun): store.update_agent_snapshot + executor.rerun_step(×1.5 cap 40)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: `POST /steps/{step}/rerun` 端点

**Files:**
- Modify: `backend/src/porto_chatbot/api/routes/workflow.py`（新增路由）
- Test: `backend/tests/test_workflow_api.py`（追加）

**Interfaces:**
- Consumes: T7（`executor.rerun_step`、`WorkflowRunning`）
- Produces: `POST /api/porto/workflows/{id}/steps/{step}/rerun` → `{workflow_id, status:"running"}`；409 = 撞顶或正在 running；400 = step 非法；404 = workflow 不存在。

- [ ] **Step 1: 写失败测试（追加到 test_workflow_api.py，按现有 fixture 风格）**

参考该文件已有的 `create_workflow` 测试风格，追加：

```python
def test_rerun_step_accepted(client_fixture_name_here, ...):
    # 创建 workflow 跑到 understand 后,POST rerun → 200 {status:"running"}
    ...
    resp = client.post(f"/api/porto/workflows/{wid}/steps/understand/rerun")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


def test_rerun_step_bad_step_returns_400(...):
    resp = client.post(f"/api/porto/workflows/{wid}/steps/nope/rerun")
    assert resp.status_code == 400


def test_rerun_step_at_cap_returns_409(...):
    # 把 agent_snapshot.agent_max_tool_turns 设为 hard_cap 后 rerun → 409
    ...
    assert resp.status_code == 409
```

> 实现者：照 `test_workflow_api.py` 现有 fixture（client / store / executor 注入）补全 setup；断言三态。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_workflow_api.py -v -k rerun`
Expected: FAIL（路由不存在 → 404）

- [ ] **Step 3: 改 `api/routes/workflow.py`**

顶部 import `STEPS`：

```python
from ...agent.graph import STEPS
```

在 `advance_workflow` 路由后新增：

```python
@router.post("/api/porto/workflows/{workflow_id}/steps/{step}/rerun", response_model=WorkflowCreated)
def rerun_step(workflow_id: str, step: str):
    """整步重跑:turn ×1.5(ceil) cap tool_turn_hard_cap,绕过 graph 调节点函数。

    - 404: workflow 不存在
    - 400: step 不在 STEPS
    - 409: 已达 turn 硬上限(引导手编)或 workflow 正在 running
    - 200: 已接受,后台 worker 重跑该步
    """
    store = get_workflow_store()
    if store.get(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    if step not in STEPS:
        raise HTTPException(400, f"step must be one of {STEPS}")
    try:
        get_workflow_executor().rerun_step(workflow_id, step)
    except WorkflowRunning:
        raise HTTPException(409, "workflow is running or turn limit reached") from None
    return WorkflowCreated(workflow_id=workflow_id, status="running")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_workflow_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/api/routes/workflow.py backend/tests/test_workflow_api.py
git commit -m "feat(api): POST /steps/{step}/rerun 整步重跑端点

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: 前端默认值 + rerun API client

**Files:**
- Modify: `frontend/src/lib/api.ts:246`（默认 4→10）+ 新增 `rerunStep`
- Modify: `frontend/src/lib/types.ts`（加 `tool_meta` 类型，若需）

**Interfaces:**
- Consumes: T8（rerun 端点）
- Produces: `rerunStep(workflowId, step)`；默认 `agent_max_tool_turns=10`。

> 前端无单测；验证 = `npm run build`。

- [ ] **Step 1: 改 `api.ts`**

`api.ts:246` 把

```typescript
  agent_max_tool_turns: 4,
```

改为

```typescript
  agent_max_tool_turns: 10,
```

在现有 `advanceWorkflow`（或同类 workflow API 函数）旁新增：

```typescript
export async function rerunStep(workflowId: string, step: string): Promise<{ workflow_id: string; status: string }> {
  const resp = await fetch(`${API_BASE}/api/porto/workflows/${workflowId}/steps/${step}/rerun`, {
    method: "POST",
  });
  if (resp.status === 409) {
    throw new Error((await resp.json()).detail ?? "running or turn limit reached");
  }
  if (!resp.ok) throw new Error(`rerun failed: ${resp.status}`);
  return resp.json();
}
```

> 实现者：按 `api.ts` 现有 fetch 封装风格（API_BASE / 错误处理）对齐，函数名/签名照上面。

- [ ] **Step 2: 加类型（types.ts）**

若 `types.ts` 有 `WorkflowOutput` 类似类型，给 output 加可选 `tool_meta`：

```typescript
export interface ToolMeta {
  turns?: number;
  tool_calls?: number;
  truncated?: boolean;
  max_turns?: number;
  reason?: string | null;
}
```

按现有类型组织贴入（不破坏既有结构）。

- [ ] **Step 3: build 验证**

Run: `cd frontend && npm run build`
Expected: 成功（无类型错误；eslint 假阳性以 build 为准）

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/lib/types.ts
git commit -m "feat(frontend): agent_max_tool_turns 默认 10 + rerunStep API

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 10: 前端卡片标记 + 重跑按钮（三态）

**Files:**
- Modify: `frontend/src/components/porto-workbench.tsx`（subsystem 卡片 + step 工具栏）

**Interfaces:**
- Consumes: T6（`outputs[step].output.tool_meta`、`spec_results[name].tool_meta`）、T9（`rerunStep`）
- Produces：截断 subsystem 卡片 = 左色条 + 底部状态行 + ⓘ tooltip；generate step 工具栏 = 红 chip「n/total 超限」+「⟳ 重跑本步」按钮（可重跑/loading/撞顶三态）。

> 前端无单测；验证 = `npm run build` + 手动点开出错 workflow 看视觉。

- [ ] **Step 1: 卡片标记（B 方案）**

定位 `porto-workbench.tsx` 中渲染 subsystem spec 卡片的 map（约 `agentDraft` / specs 渲染处）。对每个 subsystem，从 `spec_results[name]?.tool_meta` 取截断态：

```tsx
const tm = specResults?.[name]?.tool_meta;
const truncated = !!tm?.truncated;
```

卡片根元素加样式（truncated 时）：

```tsx
<div style={{
  border: "1px solid #e5e7eb",
  borderLeft: truncated ? "4px solid #ef4444" : "1px solid #e5e7eb",
  borderRadius: 8, padding: 16, background: "#fff",
}}>
  {/* 标题 + 正文 */}
  {truncated && (
    <div style={{
      marginTop: 12, paddingTop: 10, borderTop: "1px dashed #fca5a5",
      fontSize: 12, color: "#b91c1c", display: "flex", alignItems: "center", gap: 6,
    }}>
      ⚠️ 工具超限 {tm.tool_calls}/{tm.max_turns}
      <span title={`工具调用达上限 ${tm.tool_calls}/${tm.turns} turn · max_turns=${tm.max_turns}`}
            style={{ borderBottom: "1px dotted #b91c1c", cursor: "help" }}>ⓘ 详情</span>
    </div>
  )}
</div>
```

- [ ] **Step 2: step 工具栏重跑按钮（generate + 单步）**

在 generate step 标题行加汇总 chip + 重跑按钮：

```tsx
const genMeta = outputs?.generate?.output?.tool_meta;  // {truncated: any}
const anyTrunc = !!genMeta?.truncated;
const curMax = agentDraft.agent_max_tool_turns;        // 当前 snapshot 值
const newMax = Math.min(Math.ceil(curMax * 1.5), 40);  // 前端预算显示(后端为准)
const atCap = curMax >= 40;
const [rerunning, setRerunning] = useState(false);

{anyTrunc && (
  <span style={{ fontSize: 12, color: "#b91c1c",
    border: "1px solid #fca5a5", background: "#fef2f2",
    padding: "3px 9px", borderRadius: 999 }}>{truncCount}/{total} 子系统超限</span>
)}
<button
  disabled={rerunning || atCap}
  title={atCap ? "已达 turn 硬上限(40)，请检查 prompt 或手动编辑产出"
              : `${curMax}→${newMax} turn · 上限 40`}
  onClick={async () => {
    setRerunning(true);
    try { await rerunStep(workflowId, "generate"); await refreshWorkflow(); }
    catch (e) { setError(String(e)); } finally { setRerunning(false); }
  }}
  style={{
    background: rerunning ? "#9ca3af" : atCap ? "#f3f4f6" : "#2563eb",
    color: rerunning || atCap ? "#fff" : "#fff",
    cursor: rerunning || atCap ? "not-allowed" : "pointer",
    border: "1px solid transparent", borderRadius: 8, padding: "7px 14px", fontSize: 13,
  }}>
  {rerunning ? <><Spinner/> 重跑中…</>
   : atCap ? "重跑本步 · 已达上限"
   : "⟳ 重跑本步"}
</button>
```

`Spinner` 用项目既有 spinner 或一个 CSS 旋转圆点。understand/identify 单步截断时（`outputs[step].output.tool_meta?.truncated`）同样渲染该按钮，step 参数换成对应步名。

> 实现者：按 `porto-workbench.tsx` 既有状态管理（`agentDraft` / `outputs` / `refreshWorkflow` 等实际命名）对齐变量名；上面是结构示意，落地时匹配现有代码风格。

- [ ] **Step 3: build 验证**

Run: `cd frontend && npm run build`
Expected: 成功

- [ ] **Step 4: 手动验证（可选但推荐）**

启动 backend + frontend，造一个会截断的 workflow（低 max_turns + 复杂 PRD），确认：卡片左色条 + 底部状态行 + ⓘ tooltip；重跑按钮 → loading → 完成刷新。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/porto-workbench.tsx
git commit -m "feat(frontend): 截断卡片左色条标记 + step 重跑按钮三态

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 执行顺序与依赖

```
T1 (client 截断) ──┬─→ T4 (generate loop) ──→ T6 (投影) ──┐
T3 (SpecResult) ───┘                                      ├─→ T10 (前端 UI)
T5 (understand) ──────────────────────────────────────→ T6 ┘
T2 (settings) ───┬─→ T7 (store+rerun) ──→ T8 (API) ──→ T9 (前端 api) ──→ T10
```

可并行：T1 / T2 / T3 互不依赖；T4 等 T1+T3；T5 等 T1；T6 等 T3+T5；T7 等 T2。

## Self-Review 结论

- **Spec 覆盖**：spec §4.1→T1、§4.5→T2、§6 SpecResult→T3、§4.3→T4、§4.2→T5、§4.4→T6、§4.6+store→T7、§7→T8、前端默认→T9、§5→T10。无遗漏。
- **Placeholder**：T8/T9/T10 对前端现有 fixture / 变量名给了"按现有风格对齐"的指引（因未读全 test_workflow_api fixture 与 porto-workbench 全貌），但断言、签名、样式值均已给齐，实现者无需猜测行为。
- **类型一致**：`tool_meta` 结构（turns/tool_calls/truncated/max_turns/reason）全任务统一；`_next_max_turns`/`_apply_new_max`/`rerun_step`/`_run_rerun` 命名一致；`WorkflowRunning` 复用于撞顶 409。
