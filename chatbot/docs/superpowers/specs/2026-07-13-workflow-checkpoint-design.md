# Porto Chatbot 分步交互式 Workflow 设计

- 日期:2026-07-13
- 状态:已评审,待实现
- 方案:B(自写状态机,移除 langgraph)

## 背景与动机

当前 PRD 拆解([graph.py](../../../backend/src/porto_chatbot/agent/graph.py))用 langgraph 把 `retrieve→understand→identify→generate→evaluate` 编排成**全自动一次性执行**的同步 graph。前端"运行拆解"按钮 `POST /api/porto/workflows` 同步等待完整结果。两个问题:

1. **超时**:一次拆解可达分钟级(实测 8 分钟,主要耗在 generate_specs),HTTP 长连接被 next dev 代理掐断,前端报 "Internal Server Error",而后端实际跑完(api.log 仅 200 记录)。根因叠加:LLM 调用未设 timeout、6 子系统并行 spec loop、代理层超时阈值短于实际耗时。
2. **零交互**:用户无法在 identify 出子系统后审视/修正,错误理解污染后续所有步骤。

本设计把 workflow 改成**分步、可暂停、可编辑、可续跑**的交互式流程,解决超时(异步轮询 + LLM timeout)和交互缺失(checkpoint)。

## 目标

- workflow 按步骤推进;关键步骤(understand/identify/generate)跑完**暂停**,产出可审视/编辑/保存,点"继续"才走下一步
- sqlite 持久化 workflow 状态 + 各步产出,支持**断点续跑**(服务重启/刷新后从中断处继续)
- **异步后台执行 + 前端短轮询**,LLM 调用加 timeout,根治长连接被掐
- spec 并发生成(可配,默认 3、上限 10)
- generate_specs 的 spec loop 算法保持不变

## 非目标

- 不做多 agent / 复杂路由(保留单线流程)
- 不做"无编辑的纯重放/分支"(改产出触发回退已够)
- 不做旧数据迁移、不做向后兼容(从头开始)

## 已确认决策

| 决策点 | 选择 |
|---|---|
| checkpoint 步骤 | understand / identify / generate;retrieve、evaluate 自动过 |
| 编辑语义 | 覆盖该步产出 + current_step 回退到该步 + 删除其后产出 |
| sqlite 能力 | 历史 + 断点续跑 |
| 传输模型 | 异步后台执行 + 前端短轮询(不上 SSE、不逐字流式) |
| spec 并发 | `spec_refine_concurrency`(int,默认 3,1–10)替换 `spec_refine_parallel`(bool) |
| 编排方案 | B:自写状态机,移除 langgraph |
| 服务重启恢复 | 标 `interrupted`,不回退 current_step,用户手动 advance |
| 兼容性 | 无,旧数据归档不迁移 |

## 1. 后端架构

### 1.1 状态机 `WorkflowRunner`(替换 graph.py 的 langgraph 编排)

纯逻辑,不碰线程/IO,易测。

```
STEPS = [retrieve, understand, identify, generate, evaluate]
CHECKPOINTS = {understand, identify, generate}

advance(state):
  start_idx = index(STEPS, state.current_step) + 1   # current_step=None → 0
  for step in STEPS[start_idx:]:
    output = run_node(step, state)          # 复用 nodes/*.py(签名 node(agent,state)->state 不变)
    state.outputs[step] = (output, "ai")
    state.current_step = step
    if step in CHECKPOINTS:
      state.status = "awaiting_input"
      return state
  state.status = "completed"
  return state
```

- 节点函数整块复用 [nodes/*.py](../../../backend/src/porto_chatbot/agent/nodes/)、[specs/](../../../backend/src/porto_chatbot/specs/)、heuristics,不动
- 抛弃 graph.py 的 `StateGraph` + 条件回边;checkpoint 模式下回边由用户手动(回 identify 编辑再继续),不再自动回边
- 抛弃后 langgraph 仅 graph.py 使用,可移除依赖

### 1.2 执行器 `WorkflowExecutor`(单例,后台线程)

- `advance(workflow_id)` 提交后台线程立即返回;线程内:加载 state → runner.advance → 每步跑完落库
- 同一 workflow 加锁,`running` 时再 advance 返回 409
- 不同 workflow 独立线程,可并存
- `generate_specs` 节点内 `ThreadPoolExecutor(max_workers=min(spec_refine_concurrency, len(subs)))`(复用现有并行框架,仅换并发数来源)

### 1.3 持久化(两表,`~/.porto/workflows.sqlite3`,开 WAL)

```sql
CREATE TABLE workflows (
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
);

CREATE TABLE workflow_outputs (
  workflow_id TEXT NOT NULL,
  step_name   TEXT NOT NULL,
  output      TEXT NOT NULL,
  produced_by TEXT NOT NULL,
  produced_at TEXT NOT NULL,
  PRIMARY KEY (workflow_id, step_name)
);
```

- `status` ∈ {created, running, awaiting_input, completed, failed, interrupted}
- `current_step` ∈ {NULL, retrieve, understand, identify, generate, evaluate};**只在节点跑完后更新**,故中断时停在最后完成的步
- `output` 的 JSON 结构按 step:
  - retrieve → `{"sources": [...]}`
  - understand → `{"understanding": "..."}`
  - identify → `{"subsystems": [...]}`
  - generate → `{"specs": {name: text}, "attempts": {...}}`
  - evaluate → `{"evaluation": {...}}`
- resume:读 `workflows` + `workflow_outputs` 重建 state,接 advance
- 历史列表:`SELECT ... FROM workflows ORDER BY created_at DESC`

### 1.4 配置快照

advance 用创建时存的 `rag_snapshot`/`agent_snapshot` 重建 `PortoAgent`(LLMClient + vector_store)。用户中途改设置不影响进行中的 workflow。每次 advance 重建 agent(频率低,开销可接受)。

## 2. REST API

### 2.1 endpoints

| 方法 路径 | 作用 | 返回 |
|---|---|---|
| POST /api/porto/workflows | 创建:存 PRD+配置快照,后台跑 retrieve→understand | `{workflow_id, status:"running"}` |
| POST /api/porto/workflows/upload | 上传文件→抽文本→等价上面 | 同上 |
| GET /api/porto/workflows | 列表(可按 session_id/status 过滤) | `[{workflow_id,project_name,status,current_step,created_at,score?}]` |
| GET /api/porto/workflows/{id} | **轮询主入口**:状态+全部 steps outputs | 完整详情 |
| POST /api/porto/workflows/{id}/advance | 继续到下个 checkpoint(后台) | `{status:"running"}` |
| PUT /api/porto/workflows/{id}/steps/{step} | 保存用户编辑 | 更新后详情 |
| DELETE /api/porto/workflows/{id} | 删除 | 204 |

### 2.2 典型数据流

```
1. 提交 PRD    POST /workflows         → {id, running};后台跑 retrieve→understand
2. 轮询        GET /workflows/{id} 每2s  → running/current_step=understand → 前端"生成中"
3. understand 完(后台落库)              → awaiting_input → 轮询拿到 understanding 产出
4. 用户看/改   (可选)PUT /steps/understand → 覆盖产出(produced_by=user)
5. 点继续      POST /advance            → 后台跑 identify → identify checkpoint
6. identify/generate 重复
7. generate 后继续 POST /advance         → evaluate 自动跑 → completed
```

轮询:`status=running` 每 2s 一次;`awaiting_input/completed/failed/interrupted` 停。

### 2.3 编辑触发回退(PUT /steps/{step})

1. 覆盖该步 `output`(`produced_by=user`)
2. `current_step` 回退到该 step
3. 删除该 step **之后**的全部 outputs(失效)
4. `status=awaiting_input`(停在改的步等用户继续)

例:在 generate checkpoint 改 identify → current_step=identify,删 generate/evaluate outputs,下次 advance 从 generate 重跑(基于新 subsystems)。

### 2.4 并发与 resume

- 同一 workflow:executor 锁,running 时 advance 返回 409
- 跨 workflow:独立线程,sqlite WAL
- 服务重启:启动扫 `status=running` → 改 `interrupted`(current_step/outputs 不动)→ 用户 advance 从 current_step 下一步恢复
- failed:用户 advance 从 current_step 下一步重跑失败步

## 3. 前端交互([porto-workbench.tsx](../../../frontend/src/components/porto-workbench.tsx))

把 WorkflowPanel 从"一次性运行"重构为分步向导。

### 3.1 进度可视化

顶部步骤指示器,5 步:
```
✓ retrieve(自动) → ● understand[待审] → ○ identify → ○ generate → ○ evaluate(自动)
```
每步状态:pending / running(spinner) / done(✓) / checkpoint(可点回退编辑)。

### 3.2 checkpoint 产出区(按 current_step 切换)

| checkpoint | 形态 | 交互 |
|---|---|---|
| understand | markdown 报告 | 默认 ReactMarkdown 只读,点"编辑"切 textarea |
| identify | 子系统卡片列表 | 增删改 name/responsibility/capabilities/type |
| generate | N 份 spec | 标签页/折叠列表,每份 ReactMarkdown + 可编辑 |

- running 时显示"生成中…"(spinner),轮询中
- awaiting_input 时展示产出 + 「保存修改」「继续下一步」
- completed 时全展示 + evaluate 分数

### 3.3 轮询与按钮

- `useWorkflowPolling(id)`:running 时每 2s GET /workflows/{id},其它停
- 「保存修改」→ PUT /steps/{step}(有改动时启用)
- 「继续下一步」→ POST /advance → running → 轮询接管

### 3.4 历史列表

Sidebar 新增 "Workflows" section(仿 Chat Records):列最近 N 个 workflow,点击切换详情。进行中可继续,完成可回看。按 session_id 过滤。

### 3.5 spec 并发度配置

AgentSettingsForm 高级 subTab:「Spec 并行生成」checkbox → 「Spec 并发度」number input(1–10,默认 3)。

> 前端 [AGENTS.md](../../../frontend/AGENTS.md) 提示本仓库 Next.js 是定制版本,实现阶段先读 `node_modules/next/dist/docs/` 相关指南,不凭训练数据写。

## 4. 错误处理与配套修复

### 4.1 失败/中断

- 后台线程抛异常 → catch → `status=failed` + `error` + current_step 不动 → 前端轮询见 failed → 显示错误 + 「重试」→ advance 重跑
- 节点内部降级保留([understand/identify fallback](../../../backend/src/porto_chatbot/agent/nodes/understand.py)、[generate template 降级](../../../backend/src/porto_chatbot/specs/loop.py)):返回产出不抛异常,不进 failed
- interrupted:服务重启恢复的 running(见 2.4)

### 4.2 LLM 调用加 timeout(诊断根因配套)

异步解决了"前端不等",但单次 LLM 调用挂死会让后台线程永久 running。给 [llm/client.py](../../../backend/src/porto_chatbot/llm/client.py) 的 `OpenAI()`/`Anthropic()` 构造传 client 级 `timeout=`。新增配置 `agent_request_timeout`(默认 120s,可配),所有 complete* 继承。挂死 → failed 而非永久 running。

### 4.3 测试策略

- `WorkflowRunner`(纯逻辑,mock 节点):状态转换、checkpoint 停止位置、advance 起点、回退失效删后续 outputs
- `WorkflowStore`(sqlite):CRUD、resume 重建、WAL 并发读写
- API 集成(沿用 [test_api.py](../../../backend/tests/test_api.py) 风格):创建→轮询→编辑→继续→回退→completed 全流程;409 并发保护;failed 重试;interrupted 恢复
- 节点函数已有,不动

### 4.4 迁移与依赖

- 旧 `~/.porto/workflows/{uuid}/` 文件产出**不迁移**,留作只读归档;新 workflow 全走 sqlite 空库
- `spec_refine_parallel` → `spec_refine_concurrency`:直接替换,无兼容逻辑
- 移除 langgraph 依赖(graph.py 删除后无其他引用)
- `WorkflowResponse` 模型保留(completed 详情用),创建 endpoint 改返回 `{workflow_id, status}`;前后端同步改,无外部消费者

## 影响面

**新增**(backend/src/porto_chatbot/):
- `workflow_store.py` — sqlite 两表 + resume 重建
- `workflow_runner.py` — 状态机
- `workflow_executor.py` — 后台线程 + 锁

**改**:
- `api/routes/workflow.py` — 新 endpoint 群
- `settings.py` — +agent_request_timeout,+spec_refine_concurrency,−spec_refine_parallel
- `models/payload.py` — AgentSettingsPayload 同步
- `llm/client.py` — OpenAI/Anthropic timeout
- `agent/nodes/generate.py` — 并发数来源改 spec_refine_concurrency
- `main.py`/`api/app.py` — 启动扫 running→interrupted;注册 executor 单例
- `tests/test_api.py` — workflow 新流程

**删**:
- `agent/graph.py` — langgraph 编排
- `pyproject.toml` — langgraph 依赖

**前端**:
- `components/porto-workbench.tsx` — WorkflowPanel 重构 + 历史 sidebar
- `lib/api.ts` — 新 workflow API client
- `lib/types.ts` — spec_refine_concurrency + workflow 详情类型
