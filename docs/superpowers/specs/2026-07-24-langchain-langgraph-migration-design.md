# LangChain / LangGraph 迁移设计

- **日期**:2026-07-24
- **状态**:已与用户对齐,待 writing-plans 出实现计划
- **范围**:backend `porto_chatbot` 的 LLM client 层 + agent/workflow 编排层
- **不影响**:前端、API 契约、检索层(llama-index)

---

## 1. 背景

### 1.1 现状

`porto_chatbot` 后端的 LLM 抽象层 [`llm/client.py`](../../backend/src/porto_chatbot/llm/client.py) 的 `LLMClient` 直接使用 `anthropic.Anthropic` / `openai.OpenAI` 原生 SDK,并手写了大量本应由框架承担的逻辑:

- **两套 provider 消息格式互转**(`_openai_text` / `_anthropic_text` / `_openai_tool_step` / `_anthropic_tool_step` / `_append_assistant_tool_step` / `_append_tool_result`,约 120 行)
- **手写 tool calling 循环**([`complete_with_tools`](../../backend/src/porto_chatbot/llm/client.py#L161-L229))
- **手写 JSON 结构化输出**解析 + 重试([`complete_structured`](../../backend/src/porto_chatbot/llm/client.py#L128-L156))

编排层 [`WorkflowRunner`](../../backend/src/porto_chatbot/workflow_runner.py) 是纯 Python for 循环状态机,配套 [`WorkflowExecutor`](../../backend/src/porto_chatbot/workflow_executor.py) 手写的 state 重建(`_rebuild_state`)、按步截取落库(`_persist_state`)、回退清下游(`clear_outputs_after`)、崩溃恢复(`mark_running_interrupted_on_startup`)。这些手写状态机逻辑复杂且易出 bug。

依赖现状:`langchain-text-splitters`(仅文档切分)+ llama-index 全家桶(检索/RAG)+ 原生 anthropic/openai SDK(LLM)。**无 langgraph**。

### 1.2 配置层已为 langchain 铺好

[`settings.py`](../../backend/src/porto_chatbot/settings.py) 的 env 别名**早已是 `LANGCHAIN_*` 前缀**(`LANGCHAIN_AGENT_PROVIDER` / `LANGCHAIN_API_KEY` / `LANGCHAIN_BASE_URL` / `LANGCHAIN_MODEL` / `LANGCHAIN_TEMPERATURE` / `LANGCHAIN_MAX_TOKENS`),配合 `ConfigStore` db 覆盖层与 `runtime_settings_from_snapshot` 快照重建,langchain `ChatModel` 初始化所需字段(provider/api_key/base_url/model/temperature/max_tokens/timeout)一个不少。配置层零改动。

### 1.3 纠正一条过时记忆

历史记忆记载"agent 架构已改造为 LangGraph 条件图",但当前代码**完全没有 langgraph**:`WorkflowRunner` 是纯 for 循环,`agent/graph.py` 不存在,依赖里也无 langgraph。本设计是首次真正引入 langgraph。

---

## 2. 目标与范围

### 2.1 目标

将 client 底层换为 langchain `ChatModel`,将 workflow 编排换为 langgraph `StateGraph`,用上 langgraph 的 checkpoint / interrupt / resume / map-reduce 高级能力,删掉手写的 provider 适配与状态机代码。

### 2.2 三层范围(已确认:L1 + L2 + L3)

| 层 | 内容 |
|---|---|
| **L1** | `LLMClient` 内部换 langchain `BaseChatModel`,6 个方法 + 2 个属性签名不变 → 8 处调用方零改动 |
| **L2** | `WorkflowRunner` → langgraph `StateGraph`;`interrupt` + `SqliteSaver` 替换手写 checkpoint / advance / PUT / resume / 崩溃恢复 |
| **L3** | spec refine loop 做成子图;generate 节点用 `Send` 做 map-reduce 并行 |

### 2.3 不做的事(YAGNI / 已确认保留)

- **检索层保留 llama-index**:`retrieval.py` / `vector_store` / `bm25_index` / `index_supervisor` 不动。工具 `search_knowledgebase` 的 handler 仍调 llama-index,只是工具定义走 langchain。
- **chat 路由不做 langgraph**:chat 是单次请求-响应的 RAG QA + streaming,无 checkpoint 需求。只通过 L1 享 langchain 底层。
- **不迁异步**:保持同步(langgraph sync API + 现有 threading/guard)。
- **tool calling 不上 `ToolNode`**:保持封装在 `LLMClient.complete_with_tools` 内(`bind_tools` 循环),不拆到 graph 层。
- **API 契约不变**:7 个 workflow endpoints + chat endpoints 对前端契约不变。
- **保留降级**:`agent.llm.enabled=False` 时各节点走模板/规则 fallback,系统不崩。

---

## 3. 设计决策记录

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 检索层 | 保留 llama-index | 用户决定不迁移;半统一混合栈长期是负债,但检索迁移风险/收益不划算 |
| D2 | chat 路由 | 不做 graph | 无 checkpoint/interrupt/resume 需求 |
| D3 | 同步/异步 | 保持同步 | 全栈 async 风险高,超出"换 langchain"本意 |
| D4 | API 契约 | 不变 | 前端零改动 |
| D5 | 降级 | 保留"无 LLM 也能跑" | 硬约束 |
| D6 | 持久化策略 | **方案 A 双层** | langgraph `SqliteSaver` 管 graph state;`WorkflowStore` 瘦身为业务视图层 |
| D7 | spec loop 子图深度 | **深子图 + Send map-reduce** | 删掉手写 `ThreadPoolExecutor` 并行,声明式 map-reduce |
| D8 | tool calling | 封装在 LLMClient 内 | 节点内 mini-agent loop 不需要跨节点编排 |
| D9 | structured output | 保留"prompt 注入 schema + JSON 解析重试"土办法 | provider 无关、已验证可靠;`with_structured_output` 对 openai-like 自建端点支持不稳 |
| D10 | `complete_document` | 允许混合 fallback | langchain 对 provider 特定 PDF block 支持深度不一;不行则该方法保留原生 SDK |

---

## 4. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│  FastAPI routes (chat / workflow / settings / ...)      │  ← 契约不变
├─────────────────────────────────────────────────────────┤
│  langgraph 编排层 (NEW)                                 │
│    WorkflowGraph: retrieve→understand→identify→         │  ← L2
│                   generate→evaluate (+interrupt_after)  │
│    spec 子图 + Send map-reduce (generate 内)            │  ← L3
│    SqliteSaver (checkpointer, 独立 db)                  │
├─────────────────────────────────────────────────────────┤
│  LLMClient (接口不变, 内部换 langchain)                 │  ← L1
│    BaseChatModel: ChatOpenAI / ChatAnthropic            │
│    bind_tools / stream / invoke                         │
├─────────────────────────────────────────────────────────┤
│  检索层 (保留)                                           │
│    llama-index: chroma + bm25 + hybrid + rerank         │  ✋ 不动
│    WorkflowStore (瘦身): 业务元数据 + 审计 + 投影        │
└─────────────────────────────────────────────────────────┘
```

---

## 5. L1 — LLMClient 的 langchain 重写

### 5.1 核心约束

`LLMClient` 的公共接口**完全不变**:

- 方法:`complete` / `complete_structured` / `complete_with_tools` / `stream` / `complete_document`
- 属性:`enabled` / `document_capabilities`
- 类型:`ToolDef` / `ToolCall` / `ToolLoopResult` / `Message` / `ModelCapabilities`([`llm/types.py`](../../backend/src/porto_chatbot/llm/types.py))

8 处调用方(`api/routes/chat.py` / `workflow.py` / `intent.py` / `memory/compaction.py` / `documents.py` / `agent/agent.py` / `workflow_executor.py` / `specs/`)零改动。

### 5.2 provider 映射

`_build_client`([client.py:459-481](../../backend/src/porto_chatbot/llm/client.py#L459-L481))重写:

| `agent_provider` | langchain 类 | base_url |
|---|---|---|
| `openai` | `ChatOpenAI` | 有则当 openai-compatible 端点 |
| `anthropic` | `ChatAnthropic` | 有则传入 |

`_build_critic_llm`([agent.py:40-64](../../backend/src/porto_chatbot/agent/agent.py#L40-L64))用同机制独立构造 critic。

### 5.3 方法实现映射

| 方法 | 迁移后 |
|---|---|
| `complete` | `model.invoke(messages).content` |
| `complete_structured` | 保留 prompt 注入 schema + `_try_parse_json` 重试;底层走 `invoke`(**不**用 `with_structured_output`,见 D9) |
| `complete_with_tools` | `model.bind_tools(langchain_tools)` 循环;`ToolDef` → `StructuredTool.from_function` 内部转换;**删掉两套 provider 适配** |
| `stream` | `model.stream()` |
| `complete_document` | 构造多模态 `HumanMessage`(content blocks);**风险见 D10**,spike 后定 langchain 还是保留原生 |
| `enabled` / `document_capabilities` | 逻辑保留(langchain 不暴露 PDF 能力判断) |

### 5.4 ToolDef → langchain Tool

[`tools/registry.py`](../../backend/src/porto_chatbot/tools/registry.py) 仍产出 `list[ToolDef]`(节点零改动)。`LLMClient.complete_with_tools` 内部把 `ToolDef` 转 `StructuredTool.from_function`,handler 闭包仍调 llama-index 检索。

### 5.5 配置兼容性

`ChatModel` 初始化字段全部来自 `Settings`(env `LANGCHAIN_*`)+ `ConfigStore` db 覆盖 + `runtime_settings_from_snapshot` 快照。**`settings.py` 零改动。**

---

## 6. L2 — WorkflowRunner → langgraph StateGraph

### 6.1 Graph 拓扑

```
START → retrieve → understand → identify → generate → evaluate → END
                         ▲               ▲             ▲
                    interrupt_after  interrupt_after  interrupt_after
```

`interrupt_after=["understand", "identify", "generate"]` —— 一比一替换 `CHECKPOINTS`。`STEPS` / `CHECKPOINTS` 常量从 `WorkflowRunner` 迁入 graph 定义。

### 6.2 State + reducer

`PortoAgentState`([state.py](../../backend/src/porto_chatbot/agent/state.py))扩展并配 reducer:

| 字段 | reducer | 说明 |
|---|---|---|
| `current_step` / `status` | last-write-wins | 新增,替代 runner 维护键 |
| `steps: list[AgentStep]` | `add`(append) | 各节点 append 日志 |
| `specs: dict` / `spec_results: dict` | dict-merge | spec 子图并行写 + `PATCH /specs` 改单 key |
| 其余字段 | last-write-wins | sources/understanding/subsystems/evaluation 等 |

### 6.3 checkpointer + agent 注入

- `SqliteSaver` 落到 `~/.porto/langgraph_checkpoints.sqlite`,**独立于** `workflows.sqlite3`
- `thread_id = workflow_id`
- `config = {"configurable": {"thread_id": wid, "agent": agent}}`
- `agent`(`PortoAgent`,含从 snapshot 重建的 settings/llm/vector_store/critic_llm)经 `configurable` 注入

节点签名变化(逻辑不变,只改入口):

```python
# 现状: def understand_prd(agent, state) -> state
def understand_node(state, *, config) -> dict:        # 返回局部更新
    agent = config["configurable"]["agent"]
    ...  # understand_prd 原逻辑
    return {"understanding": text, "steps": [AgentStep(...)]}
```

### 6.4 操作映射

| 操作 | 现状(手写) | 迁移后(langgraph) |
|---|---|---|
| **start** | 后台线程 + `_rebuild_state` + `run_to_next_checkpoint` | 后台线程 + `graph.invoke(initial_state, config)` 跑到首个 interrupt |
| **advance** | guard 锁 +跑到下个 checkpoint | guard 锁 + `graph.stream(None, config)` 续跑 |
| **PUT /steps**(编辑+回退) | `save_output` + `clear_outputs_after` + 改 status/current_step | `graph.update_state(config, {field:val}, as_node="understand")`,图位置回退、下游重算 |
| **PATCH /specs**(微调) | 直接改 db json | `graph.update_state(config, {"specs":{name:body}})`,靠 dict-merge reducer,不回退图位置 |
| **崩溃恢复** | `mark_running_interrupted_on_startup` | 启动扫 `status="running"`:checkpointer 处于 interrupt → 标 `awaiting_input`;否则标 `interrupted` 让用户手动 advance |

**保留**:`WorkflowExecutor` 的 per-workflow `guard` 锁 + daemon 线程(langgraph 不管"同一 workflow 不能并发 advance"业务约束);`runtime_settings_from_snapshot` 快照重建。

### 6.5 WorkflowStore 去留(方案 A 双层)

**保留并瘦身**:
- `workflows` 表(元数据 / status / current_step / error / snapshot)—— list / filter / GET 详情
- `workflow_outputs` 表(审计)—— `produced_by`(ai/user) / `produced_at`
- checkpoint 暂停后,从 `graph.get_state(config).values` **投影**到 `workflow_outputs`(`_STEP_OUTPUT_KEYS` 映射保留)
- `current_step` / `status` 从 graph state 同步回 `workflows` 表

**改造 / 删掉**:
- `WorkflowRunner` 整个类:删除(STEPS for 循环 → graph 拓扑)
- `_rebuild_state`:删除(checkpointer 持久化 state,无需从 db 重建 Pydantic 模型)
- `_persist_state`:**改造为投影函数**(非删除)——数据源从内存 state 改为 `graph.get_state(config).values`;**"按图位置截取已完成步"的逻辑保留**:以 `get_state().next` 判定当前位于哪个节点之前,只投影该位置及之前的步产出(`_STEP_OUTPUT_KEYS` 映射复用)。PUT 回退后图位置前移,投影自然只写已完成步、下游产出不写——等价 `clear_outputs_after` 的清理语义,审计层不再需要独立 SQL
- `clear_outputs_after`:删除(语义并入上面的投影)
- `update_spec`(PATCH /specs):保留直接改 `workflow_outputs`(不动 `produced_by`/`produced_at`,保留现状语义)+ 同步 `graph.update_state(config, {"specs":{name:body}})` 改 graph state(specs dict-merge);不回退图位置

### 6.6 数据流实例

```
1. POST /workflows
   → WorkflowStore.create(prd_text, snapshot)
   → executor.start_workflow(wid):
       guard.acquire → 后台线程:
         agent = PortoAgent(runtime_from_snapshot(...))
         graph.invoke({prd_text, project_name, top_k, ...}, {thread_id: wid, agent})
       → 跑 retrieve→understand,interrupt
       → 投影 understanding → workflow_outputs(produced_by="ai")
       → workflows.status="awaiting_input", current_step="understand"

2. 用户 PUT /steps/understand {understanding: 改写}
   → graph.update_state(config, {understanding: 改写}, as_node="understand")
   → 图位置回退到 understand 之后(identify 将重算)
   → 投影(produced_by="user"),清 identify 下游
   → status="awaiting_input", current_step="understand"

3. POST /advance
   → guard.acquire → 后台线程: graph.stream(None, config)
   → 从 understand 之后续跑 identify → interrupt
   → 投影 subsystems, status="awaiting_input"

4. ... generate(interrupt) → evaluate → END → status="completed"
```

---

## 7. L3 — spec refine 子图 + Send map-reduce

### 7.1 子图拓扑

```
generate_node(state) ──┬── Send("spec_subgraph", {subsystem: A}) ──┐
                       ├── Send("spec_subgraph", {subsystem: B}) ──┼──→ spec_results
                       └── Send("spec_subgraph", {subsystem: C}) ──┘   (dict-merge reducer)

  spec_subgraph:
  START → initial → critique → decide ──stop──→ END(产出 {name: SpecResult})
                     ▲               │
                     └── refine ←── continue
```

### 7.2 SpecState(子图独立)

| 字段 | 用途 |
|---|---|
| `subsystem` | 输入 Subsystem |
| `current_spec` / `best` / `best_score` | 当前版 / 历史最优 / 最优分 |
| `attempts: list[SpecAttempt]` | 评审轨迹(append reducer) |
| `used_chars` / `iter_count` / `truncated` | 预算 + 迭代 + 截断标志 |

### 7.3 decide 路由(四重终止,集中)

```python
def decide(state, *, config) -> str:
    s = config["configurable"]["agent"].settings
    last = state["attempts"][-1]
    if last.verdict == "critic_unavailable":      return "stop"
    if last.verdict == "PASS" or last.score >= s.spec_refine_pass_score: return "stop"
    if state["best_score"] >= 0 and last.score <= state["best_score"]:   return "stop"  # 分数不升
    if state["used_chars"] > s.spec_refine_budget_tokens * 4:            return "stop"  # 预算
    if state["iter_count"] >= s.spec_refine_max_iter:                    return "stop"  # max_iter
    return "continue"
```

等价 [loop.py:46-62](../../backend/src/porto_chatbot/specs/loop.py#L46-L62) 的 if 链。`critique_spec` / `refine_spec` / `generate_initial_spec` 逻辑不变,只改入参来源(SpecContext → state + config)。critique 节点用 `agent.critic_llm`。

### 7.4 并发风险与退化

sync graph 下 `Send` 多路并发行为需 spike(D-未决)。若 N 个 subsystem 串行跑,退化方案 = **节点内 `ThreadPoolExecutor` 跑子图实例**(子图复用,调度退回线程池)。`spec_refine_concurrency` 语义定为"并发度"。子图无论如何都做。

---

## 8. 降级与错误处理

### 8.1 降级(硬约束,完整保留)

graph 是纯编排,不依赖 LLM。`agent.llm.enabled=False` 时各节点走现有 fallback:

| 节点 | 无 LLM 时 |
|---|---|
| understand | `_fallback_understanding`(正则/模板) |
| identify | `DOMAIN_HINTS` 规则 |
| generate | `render_template_spec`(模板) |
| critique | 返回 `critic_unavailable` 哨兵 → 子图立即 stop |

### 8.2 错误处理

| 场景 | 处理 |
|---|---|
| LLM 超时 | `ChatModel(timeout=agent_request_timeout)` → 节点异常 → `executor._worker` catch → `status="failed"` |
| worker crash | 现有 `_worker` try/except 保留 → `status="failed"` |
| 并发 advance | guard 锁非阻塞 acquire 失败 → 409 |
| checkpointer 写失败 | SqliteSaver 异常上抛 → 同 failed 路径 |
| 崩溃恢复 | 启动只标状态,用户手动 advance 时 checkpointer 续 |

---

## 9. 测试策略

| 类别 | 测试 | 处理 |
|---|---|---|
| **API 契约** | test_workflow_api / test_api / test_sessions_api | 契约不变 → 保持绿(回归基线) |
| **节点逻辑** | test_agent / test_spec_loop | 签名 `agent→config` → 改调用方式,断言不变 |
| **编排** | test_workflow_runner / test_executor / test_store | runner 删除 → 重写为 graph 测试;executor/store 适配 |

LLM mock 从 mock 原生 SDK 改为 langchain `FakeChatModel` 或自定义 `BaseChatModel` 假实现。新增:graph interrupt/resume、`update_state`(PUT/PATCH)、spec 子图 map-reduce、checkpointer 持久化、崩溃恢复。

---

## 10. 迁移阶段(给 writing-plans)

| 阶段 | 内容 | 可独立 ship |
|---|---|---|
| **0 依赖+spike** | 加 langchain/langgraph 依赖;验证未决项(§11) | — |
| **1 L1** | `LLMClient` 内部换 `BaseChatModel`;ToolDef→StructuredTool;6 接口不变 | ✅ 底层已 langchain,编排仍手写 |
| **2 L2** | WorkflowGraph + interrupt + SqliteSaver;节点签名改造;executor 改 invoke/stream/update_state;store 瘦身投影;删 WorkflowRunner/_rebuild_state/_persist_state;崩溃恢复 | ✅ |
| **3 L3** | spec 子图(initial/critique/decide/refine);generate 节点 Send;reducer | ✅ |
| **4 清理** | 删废弃 provider 适配代码;更新 docs;全量回归 | ✅ |

每阶段结束跑全量测试保绿。阶段 0 的 spike 结果回填到 plan 的具体 task。

---

## 11. 未决项(阶段 0 spike 验证)

| # | 未决项 | 影响 | 退化方案 | 状态 |
|---|---|---|---|---|
| U1 | `Send` → 子图节点 → reducer 的 map-reduce 行为是否成立 | L3 根基 | 节点内 ThreadPool 跑子图 | 待 L3 spike |
| U2 | sync graph 下 `Send` 多路并发是否真并行 | L3 性能 | 同上,ThreadPool 兜底 | 待 L3 spike |
| U3 | `SqliteSaver` 在多 workflow 并发 daemon 线程下行为 | L2 持久化 | 自定义 checkpointer / 加串行化层 | **✅ 已验证(L2 落地)** |
| U4 | `ChatOpenAI`/`ChatAnthropic` 对 provider 特定 PDF document block 的支持 | L1 `complete_document` | 该方法保留原生 SDK(D10) | **✅ 已验证(L1 落地,D10 fallback)** |

spike 通过则按本设计推进;不通过则走对应退化方案,设计其余部分不变。

### 11.1 L2 spike 验证结论(Task 1 验证,Task 6 回填)

> spike 测试见 [`backend/tests/test_langgraph_orchestration_spike.py`](../../backend/tests/test_langgraph_orchestration_spike.py)。以下 5 项构成 Task 3–5(graph + executor)的实现依据,并在 Task 6 全量回归 + 降级冒烟中端到端复现。

- **① interrupt_after + stream(None) 续跑**: ✅ langgraph 1.2.9 下 `interrupt_after=["understand"]` 使 `invoke` 在该节点之后暂停(`get_state().next=["identify"]`),`list(graph.stream(None, config))` 续跑到下个 interrupt / END。
- **② update_state(as_node=) 回退 + 下游重算**: ✅ `update_state(config, {...}, as_node="a")` 把 `next` 重置为 a 的后继(`["b"]`),续跑时 **重跑 b**(下游重算)。支撑 §6.4 的 PUT /steps 回退语义。
- **③ configurable 注入 agent**: ✅ 节点签名 `(state, *, config)`,`config["configurable"]["agent"]` 取到注入对象(`PortoAgent`)。支撑 §6.3 的 agent 注入。
- **④ Pydantic 模型过 SqliteSaver 往返**: ✅ `SourceChunk`/`Subsystem`/`SpecResult`/`SpecAttempt` 等 `BaseModel` 经 checkpoint 序列化/反序列化后**仍是模型实例**(属性访问可用),无需 `_rebuild_state` 式 dict→model 重建。
- **⑤ 共享 SqliteSaver 多 workflow 并发**: ✅ 单 `SqliteSaver`(共享 connection,`check_same_thread=False`)在 8 个 daemon 线程(不同 thread_id)下无冲突。L2 直接用 `SqliteSaver`,不需 Async/串行化层(U3 关闭)。

**对 §6.2 的精炼**:`status` **不**进 graph state(executor 从 `get_state().next` 派生:`next` 非空且含 END 之外节点 → `awaiting_input`;`next` 为空 → `completed`;启动期无 checkpoint 且 db status=`running` → `interrupted`);仅 `current_step` 进 state。§6.2 表格中 `status` 一行应读作"`current_step` 进 state;`status` 由 executor 派生,不入 state"。

**Followup(非阻塞)**:langgraph 1.2.9 对 Pydantic 模型过 checkpoint 发 deprecation warning("Deserializing unregistered type ... add to allowed_msgpack_modules")—— 当前仅警告不阻断(Task 6 降级冒烟中实测确认)。若未来 langgraph 升级把 warning 转阻断,需注册 `porto_chatbot.models.*` 到 checkpointer 的 serde(设 `allowed_msgpack_modules`)或 pin 版本。降级冒烟实测输出:`degradation ok: awaiting_input understand`(无 LLM key,graph 走 fallback 推进到 understand)。
