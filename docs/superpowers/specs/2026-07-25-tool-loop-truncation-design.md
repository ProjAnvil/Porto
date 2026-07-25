# Tool-calling 截断治理 + 整步重跑 设计

> 日期：2026-07-25　　状态：待评审
> 触发：workflow 28383061 understand 步产出崩塌（40 字符过渡语），用户以为 workflow 卡死。

## 1. 背景与问题

Porto workflow 的 LLM 驱动节点（understand / generate 等）通过 `LLMClient.complete_with_tools` 做 tool-calling loop：LLM 自主调检索工具，直到不再发 tool_call 或撞 `agent_max_tool_turns`。

实测（`agent.log` / `llm.log`）：
- understand 步：deepseek-v4-pro 4 个 turn 调了 **11 次** `search_knowledgebase`，撞 `max_turns=4` 触顶。
- `complete_with_tools` 截断分支（`llm/client.py:241-259`）的收尾逻辑有 bug：`if not assistant_text` 守卫被最后一个 turn 的**过渡语**（"让我进一步了解…"）满足为非空，跳过了无 tools 的收尾 invoke，直接把过渡语当 `result.text` 返回。
- understand 节点拿到这段 40 字符非空文本，不走 fallback，写入 state。DB 里 `workflow_outputs.understand.understanding = "让我进一步了解 imed-process 的业务流程目录结构和钱包支付校验流程。"`。
- 截断状态只进 log（`get_component_logger` 的 `propagate=False`，连 `backend.log` 都不进），前端和 DB 无任何"超限"信号，用户无法感知更无法补救。

## 2. 目标 / 非目标

**目标**
- 截断时产出明确的**固定提示**（B 方案），不暴露 LLM 残缺过渡语。
- tool-calling 元数据（turns / tool_calls / truncated / max_turns / reason）落库到 `workflow_outputs`，前端可读。
- `agent_max_tool_turns` 默认 4 → 10（前端控件已存在，`le=20` 不变）。
- 手动**整步重跑**：用户点按钮，turn ×1.5（ceil），隐藏硬上限 40 cap，撞顶禁用。
- generate 步出错时按 subsystem 标记（per-spec），但重跑动作是 step 级（整步）。

**非目标（明确排除）**
- per-subsystem 单独重跑 —— 留待 L3（spec 子图 + Send map-reduce）把 subsystem 变成 langgraph 原生并行任务后再做。
- 自动重跑 / turn 自适应重试循环 —— 手动触发，用户知情。
- understand prompt 调优（减少 deepseek 无意义反复检索）—— 模型行为问题，本次不碰；固定提示让它"显性失败"而非"悄悄糊弄"已是改善。
- refine-loop 截断（`SpecResult.truncated` 现有语义）的视觉处理 —— 本次只管 tool-calling 截断。

## 3. 决策摘要

| 议题 | 决策 |
|------|------|
| 截断产出形态 | **B**：固定提示替换，不做收尾 invoke。`result.text=""` + `result.truncated=True` |
| 异常 vs 返回值 | 返回值表达（不 raise），避免误标 workflow `failed` |
| 固定文案归属 | 节点层各自定义，`LLMClient` 只报状态不掺业务文案 |
| 元数据粒度 | 单步节点（understand 等）单条；generate 步 **per-subsystem**（via `SpecResult.tool_meta`） |
| 元数据容器 | 单步用 `AgentStep.data`；generate 用 `SpecResult.tool_meta`（新增字段，区别于现有 refine-loop `truncated`） |
| 元数据落库 | `executor._project_state` 投影时附进 `workflow_outputs[step].output["tool_meta"]`，不动表结构 |
| 默认 turn | `agent_max_tool_turns` 默认 4 → 10（`le=20` 不变） |
| 重跑触发 | 手动（step 工具栏按钮） |
| 重跑 turn 策略 | `new_max = ceil(current × 1.5)`，cap 到 `tool_turn_hard_cap=40` |
| 硬上限可见性 | `tool_turn_hard_cap=40` 进 `Settings` + config_store，**不进** `AgentSettingsPayload`（前端无控件） |
| 重跑粒度 | 整步（generate = 对所有 subsystem 重跑，已成功的也覆盖重生成） |
| 出错标记视觉 | B：左侧红色色条 + 底部状态行 + ⓘ hover 详情 |
| 重跑按钮 | step 工具栏；简洁文案「⟳ 重跑本步」（turn 变化放 tooltip）；三态：可重跑 / loading / 撞顶禁用 |

## 4. 后端设计

### 4.1 `LLMClient.complete_with_tools` 截断分支（`llm/client.py:241-259`）

删除现有"收尾 invoke"逻辑（`if not assistant_text` 守卫 + 强制 `self._client.invoke(convo)` + 第 249 行 `block`/`b` 变量名笔误），替换为：

```python
result.truncated = True
result.text = ""          # 截断 = 无可靠产出；过渡语清空，不暴露给前端
self.logger.warning(
    "llm tool loop truncated max_turns=%s total=%s",
    resolved_turns, len(result.tool_calls),
)
return result
```

`ToolLoopResult` 已有 `truncated` / `turns` / `tool_calls` 字段，不动。`text=""` 是调用方"需走固定提示"的信号。

### 4.2 单步节点：固定提示 + `AgentStep.data` 元数据

所有 `complete_with_tools` 调用点（understand 已确认；retrieve / identify / evaluate 中存在的调用，实现时 `grep complete_with_tools` 全覆盖）统一处理。以 understand 为例：

```python
result = agent.llm.complete_with_tools(...)
tool_meta = {
    "turns": result.turns,
    "tool_calls": len(result.tool_calls),
    "truncated": result.truncated,
    "max_turns": agent.settings.agent_max_tool_turns,
    "reason": "tool_loop_truncated" if result.truncated else None,
}
if result.truncated:
    understanding = _TRUNCATED_NOTICE.format(
        calls=tool_meta["tool_calls"], limit=tool_meta["max_turns"])
else:
    understanding = (result.text or "").strip() or _fallback_understanding(state)
return {
    "understanding": understanding,
    "current_step": "understand",
    **agent._step("understand_prd", "完成业务理解报告", {
        "chars": len(understanding), "used_llm": bool(understanding) and agent.llm.enabled,
        "tool_meta": tool_meta,
    }),
}
```

固定提示文案模板（节点内常量，各自定义语气）：
- understand：`"⚠️ 业务理解未能完成：本步工具调用已达上限（{calls}/{limit} turn）。建议重跑本步。"`
- retrieve / identify / evaluate：同理，各自文案。

### 4.3 generate 路径：`generate_initial_spec` 返回 tool 元数据

现状 `generate_initial_spec(ctx, sub) -> str`（`specs/steps.py`）只返回 spec 文本，tool 截断信息丢失，loop 层静默降级到模板（`loop.py:22-23`）。

改为返回 `(spec_text, tool_meta)`：

```python
def generate_initial_spec(ctx, sub) -> tuple[str, dict]:
    ...
    result = ctx.llm.complete_with_tools(...)
    tool_meta = {turns, tool_calls, truncated, max_turns, reason}
    if result.truncated:
        return (_TRUNCATED_NOTICE_SPEC.format(...), tool_meta)
    return ((result.text or "").strip(), tool_meta)
```

`generate_spec_with_loop`（`specs/loop.py`）消费：
- 拿到 `(spec_text, tool_meta)`；`tool_meta["truncated"]` 为 True 时：spec 正文 = 固定提示，**跳过 critique / refine**（对固定提示评判无意义，省调用），`break` 出 loop。
- `SpecResult` 新增 `tool_meta: dict` 字段（见 §6），把 tool_meta 写进去。

**关键：`SpecResult` 双重截断语义**
- `SpecResult.truncated`（现有）：**refine-loop** 截断（max_iter / budget），spec 已生成但未达 PASS。语义不动。
- `SpecResult.tool_meta["truncated"]`（新增）：**tool-calling** 截断，spec 未真正生成。本次前端红边只看这个。

generate 节点 `_step.data`：
- `attempts[name] = SpecResult.model_dump()` 自动带 `tool_meta`（零额外结构）。
- step 级汇总：`any(r.tool_meta["truncated"] for r in results.values())` 决定是否在 step 工具栏亮红 chip。

### 4.4 `executor._project_state` 投影 `tool_meta`（`workflow_executor.py:230-267`）

投影时附加 **step 级** `tool_meta` 到每步 output（per-subsystem 的 tool_meta 已随 spec_results 自动投出，不重复处理）：

- **单步节点**：`tool_meta` 在 `AgentStep.data` 里（不在业务字段），从 `values["steps"]` 找该 step 的 `AgentStep`，取 `data["tool_meta"]`，附加为 `output["tool_meta"]`。
- **generate**：per-subsystem 的 `tool_meta` 已在 `spec_results[name]`（`SpecResult.model_dump()` 自带，由 `_STEP_OUTPUT_KEYS` 投出）——前端直接读 `output["spec_results"][name]["tool_meta"]`。投影只需**额外**附加 step 级聚合 `output["tool_meta"] = {"truncated": any(r["tool_meta"].get("truncated") for r in spec_results.values())}`，供 step 工具栏红 chip 判断。

投影结果（落 `workflow_outputs[step].output`）：

```jsonc
// understand
{"understanding": "...", "tool_meta": {"turns":2,"tool_calls":3,"truncated":false,"max_turns":10,"reason":null}}
// generate
{"specs": {...}, "spec_results": {...},
 "tool_meta": {"truncated": true, "per_subsystem": {
   "wallet-service": {"turns":4,"tool_calls":11,"truncated":true,"max_turns":10,"reason":"tool_loop_truncated"},
   "process-service": {"turns":2,"tool_calls":3,"truncated":false,...}}}}
```

不动 `_STEP_OUTPUT_KEYS`（仍只声明业务字段），`tool_meta` 在投影逻辑里单独附加。不动 `PortoAgentState` schema（`steps` / `spec_results` 已是 state 字段）。

### 4.5 默认值 + 隐藏硬上限（`settings.py`）

```python
agent_max_tool_turns: int = Field(default=10, ge=1, le=20)   # 4 → 10；前端可见，le=20 不变
tool_turn_hard_cap: int = Field(default=40, ge=1)            # 新增；不进 AgentSettingsPayload
```

- 前端默认值同步：`frontend/src/lib/api.ts:246` `agent_max_tool_turns: 4` → `10`。
- `tool_turn_hard_cap` 进 `Settings`，可经 config_store 覆盖（DB 层面），但**不**加进 `AgentSettingsPayload`（`deps.py` 的 `default_agent_settings` / 前端 DTO 都不映射它），前端无控件。

### 4.6 重跑端点

新增 `POST /api/porto/workflows/{id}/steps/{step}/rerun`（`api/routes/workflow.py`）：

- `step` 限 `STEPS`（retrieve/understand/identify/generate/evaluate）。
- acquire per-workflow guard（同 PUT/PATCH，advance 进行中 → 409）。
- 读 `current_max = agent_snapshot["agent_max_tool_turns"]`。
- `current_max >= tool_turn_hard_cap` → **409** `{"detail":"已达 turn 硬上限({cap})，请检查 prompt 或手动编辑产出"}`。
- 否则 `new_max = min(ceil(current_max × 1.5), tool_turn_hard_cap)`，**持久化**：更新 `workflows.agent_snapshot` 里的 `agent_max_tool_turns = new_max`（后续 step / 下次重跑基于新值）。
- 重建 agent（用 new_max）→ `config = _config(wid, agent)` → `state = graph.get_state(config).values`。
- **直接调用该步节点函数**（`_NODE_FNS[step]`，绕过 `graph.stream` —— subsystem 非 graph node，langgraph 无"原地重跑单 node"原生操作）：`partial = node_fn(state, config=config)`。
- `graph.update_state(config, partial, as_node=step)` 写回（as_node 语义同 PUT：图位置回到该步之后，下游待重算；generate 在 interrupt 处仍 awaiting evaluate 推进）。
- `_project_state`（produced_by 标 `"ai"`）+ `_sync_status`。
- 后台线程执行（同 advance），立即返回 `{workflow_id, status:"running"}`，前端轮询。

`WorkflowExecutor` 加 `rerun_step(workflow_id, step)` 方法实现上述逻辑；`_NODE_FNS` 从 `agent.graph` 导出或复用。

generate 整步重跑：node_fn 内部对所有 subsystem 重新 `generate_spec_with_loop`（用 new_max），已成功的也覆盖重生成（用户接受的 trade-off）。

## 5. 前端设计（`frontend/src/components/porto-workbench.tsx`）

### 5.1 出错 subsystem 卡片标记（B 方案）

`spec_results[name].tool_meta.truncated === true` 的卡片：

- 左侧 4px 红色色条（`border-left:4px solid #ef4444`），不包围全卡。
- 卡片底部状态行（虚线分隔）：`⚠️ 工具超限 {tool_calls}/{max_turns}` + `ⓘ 详情`（hover tooltip：`工具调用达上限 {tool_calls}/{turns} turn · max_turns={max_turns}`）。
- spec 正文区显示固定提示文案。

### 5.2 重跑按钮（step 工具栏）

generate 步标题行右侧：
- 红色汇总 chip：`{n}/{total} 子系统超限`（`any(truncated)` 为真时显示；全完成则绿 chip 或隐藏）。
- 重跑按钮「⟳ 重跑本步」，tooltip：`{cur}→{new} turn · 上限 {cap}`。
- 三态：
  - **可重跑**：蓝实心，点击 → `POST .../rerun`。
  - **重跑中**：灰 + spinner「重跑中…」，轮询 `GET /workflows/{id}` 到 `awaiting_input`。
  - **撞顶**（`cur >= 40`）：灰禁用「重跑本步 · 已达上限」，tooltip 引导手编。

understand / identify 等单步截断时，step 标题行同样显示重跑按钮（`step.tool_meta.truncated === true`）。

## 6. 数据模型变更

| 位置 | 变更 |
|------|------|
| `llm/types.ToolLoopResult` | 不动（已有 `text/tool_calls/turns/truncated`） |
| `models/spec.SpecResult` | **新增** `tool_meta: dict = Field(default_factory=dict)`；现有 `truncated`（refine-loop）语义不动 |
| `agent/agent.AgentStep.data` | 不动（`dict[str, Any]`，节点塞 `tool_meta` 进去） |
| `settings.Settings` | `agent_max_tool_turns` default 4→10；**新增** `tool_turn_hard_cap=40` |
| `api/deps.AgentSettingsPayload` | **不动**（不含 `tool_turn_hard_cap`，前端不可见） |
| `workflow_outputs.output`（DB TEXT/JSON） | 投影时多一个 `tool_meta` 键，零 schema 改动 |
| `workflows.agent_snapshot`（DB TEXT/JSON） | rerun 时更新其中的 `agent_max_tool_turns`，零 schema 改动 |

## 7. API 变更

| 方法 | 路径 | 说明 |
|------|------|------|
| 新增 POST | `/api/porto/workflows/{id}/steps/{step}/rerun` | 整步重跑，turn ×1.5 cap 40；409 = 撞顶或正在 running |
| 不变 | 其余 7 个端点 | 仅响应体 `outputs[step].output` 多 `tool_meta` 字段 |

## 8. 测试

1. **`llm/test_client`**：mock LLM 前 N 轮只回 tool_call（不收敛），断言 `result.truncated=True / result.text="" / result.tool_calls` 完整、`turns==max_turns`；回归正常收敛路径（某轮无 tool_call）`truncated=False / text` 非空。
2. **节点层**：mock `complete_with_tools` 返回 `truncated=True`，断言 understand 产出 = 固定提示 + `_step.data.tool_meta.truncated=True`。
3. **generate loop**：`generate_initial_spec` mock 返回 `(固定提示, tool_meta{truncated:True})`，断言 loop 跳过 critique/refine、`SpecResult.tool_meta.truncated=True`、`SpecResult.truncated`（refine）仍为默认 False。
4. **executor 投影**：构造 `state.steps` / `state.spec_results` 含 tool_meta，断言 `workflow_outputs[step].output["tool_meta"]` 正确（含 generate 的 per_subsystem 聚合）。
5. **rerun 端点**：mock executor，断言 `new_max=ceil(cur×1.5)`、agent_snapshot 持久化、撞顶（cur>=40）返回 409、advance 进行中返回 409。
6. **默认值**：`Settings().agent_max_tool_turns == 10`、`tool_turn_hard_cap == 40`、`AgentSettingsPayload` 不含 `tool_turn_hard_cap`。

## 9. 不在范围 / 后续

- per-subsystem 单独重跑：等 L3（spec 子图 + Send map-reduce）把 subsystem 变成 langgraph 原生并行任务。
- refine-loop 截断（`SpecResult.truncated`）的前端可视化（可黄色警告，后续）。
- understand prompt 调优（减少无意义反复检索）。
- `executor.log` 中大量历史 `workflow start failed`（不同 workflow，非本次问题）另行排查。

## 10. 风险与备注

- **generate 整步重跑覆盖已成功 spec**：用户接受。若反馈心疼，后续可在 rerun 时只重跑 `tool_meta.truncated=True` 的 subsystem（仍走"调函数 + update_state"，非 langgraph 机制），无需等 L3。
- **rerun 绕过 graph 直接调节点函数**：节点函数签名 `(state, *, config) -> partial` 自包含（读 state 拿上游产出），不依赖 graph 编排，可独立调用。已验证 understand / generate / retrieve 节点均读 state、无隐式 graph 依赖。
- **`tool_turn_hard_cap` 不前端暴露**：用户明确要求。若运维需调，直接改 config_store（DB）或 `.env` 的 `PORTO_CHATBOT_TOOL_TURN_HARD_CAP`。
