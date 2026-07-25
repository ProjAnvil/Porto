# LangGraph 迁移 Followup 清理(handoff prompt)

> 这不是一个实现 plan,而是给下一个 session 的**交接 prompt**。复制下方"## 给下一个 session 的 prompt"整段即可。每条 followup 给了**表现描述 + 根因判断 + 修复方向 + 涉及文件**;带 ⚠️决策点 的需先和用户定方案。

## 给下一个 session 的 prompt

继续 Porto backend langgraph 迁移的 **followup 清理**(保证无技术债)。L1(langchain client)+ L2(langgraph StateGraph 编排)已完成并合入 main(L1 @ 6187a9b、L2 @ 58a21f3、spec 文档 @ 0f31653)。L3(spec 子图 + Send map-reduce)暂不做。现在清 L2 遗留的 5 个 non-blocking followup。

## 背景
- 设计:`docs/superpowers/specs/2026-07-24-langchain-langgraph-migration-design.md`(§6 L2、§7 L3、§11 spike 全验证)
- L2 plan:`docs/superpowers/plans/2026-07-24-langgraph-orchestration-l2.md`
- L2 执行 ledger(含每 followup 的来龙去脉):`.superpowers/sdd/progress.md`
- L2 落地核心:`agent/graph.py`(STEPS/INTERRUPT_AFTER/build_workflow_graph)、`workflow_executor.py`(invoke/stream/_project_state/_sync_status/update_step/update_spec/recover_on_startup/is_any_running)、`api/deps.py`(get_checkpointer/get_workflow_graph 单例)
- 必读源码:`backend/src/porto_chatbot/{workflow_executor,workflow_store,settings,api/deps,api/app}.py` + `agent/{graph,state,agent}.py` + `tests/conftest.py`

## 环境
- 项目根:`/Users/yuhaochen/Documents/codebase/projanvil/Porto`
- venv:`cd backend && ./.venv/bin/python ...`(Python 3.14,`python` 不在 PATH)。langgraph 1.2.9 / langgraph-checkpoint-sqlite 3.1.0 / langchain-core 1.5.1。
- 从 main 开分支 `feat/langgraph-followups-cleanup`。
- ⚠️ **测试环境坑(followup 5 本身)**:`backend/.env` 若含真实 `LANGCHAIN_API_KEY`,~10 个降级测试会环境性失败。本 session 验证测试时**临时把 .env 的 `LANGCHAIN_API_KEY`/`LANGCHAIN_BASE_URL` 注释掉**(备份 `.env.bak`),验完还原 —— 跟 L2 走方案 A 一样。或先把 followup 5 修了就不用 neutralize。

## 5 个 followup(独立,建议每条一个 commit)

### F1. langgraph Pydantic checkpoint 序列化 deprecation warning
- **表现**:跑任何走 SqliteSaver checkpoint 的测试/冒烟时,stderr 出现:
  `Deserializing unregistered type porto_chatbot.models.workflow.AgentStep from checkpoint. This will be blocked in a future version. Set LANGGRAPH_STRICT_MSGPACK=true to block now, or add to allowed_msgpack_modules: [('porto_chatbot.models.workflow', 'AgentStep'), ...]`。
  功能正常(Pydantic 模型往返成功,属性访问可用),只是警告。涉及类型:`AgentStep`/`SourceChunk`/`Subsystem`/`SpecResult`/`SpecAttempt`(都在 `porto_chatbot.models.*`)。
- **根因判断**:langgraph 1.2.9 的 `SqliteSaver` 用 `JsonPlusSerializer`(msgpack)序列化 checkpoint values。Pydantic `BaseModel` 能带类型标记往返,但 serializer 对**非 langchain 内置类型**发 unregistered warning。未来版本(或开了 `LANGGRAPH_STRICT_MSGPACK=true`/升级 langgraph)会从警告变**硬阻断** → checkpoint 恢复直接失败。
- **修复方向**:给 `SqliteSaver` 注入一个显式注册了 `porto_chatbot.models.*` 的 serializer。需先 spike langgraph 1.2.9 的 API:`SqliteSaver(conn, serde=...)`?还是 serializer 的 `allowed_msgpack_modules` / 注册函数?查 `langgraph.checkpoint.sqlite.SqliteSaver` 构造签名 + `langchain_core` 的 serializer 注册机制。验证手段:修后跑 `LANGGRAPH_STRICT_MSGPACK=true pytest tests/test_langgraph_orchestration_spike.py tests/test_workflow_executor.py` 不再警告且不报错。
- **涉及**:`api/deps.py`(get_checkpointer),可能加一个 serializer 配置模块。

### F2. `STEPS` 三处定义,违反 DRY
- **表现**:`STEPS = ["retrieve","understand","identify","generate","evaluate"]` 出现在三处:
  1. `agent/graph.py` 的 `STEPS`(L2 的权威来源)
  2. `workflow_store.py` `clear_outputs_after` 里硬编码的 `order = [...]`
  3. `workflow_executor.py` `_STEP_OUTPUT_KEYS` 的 keys 隐式编码了同一顺序
  改流水线(加/删/换序步)时三处须同步,漏一处即静默 bug。
- **根因判断**:历史遗留 —— store 的 `clear_outputs_after` 早于 graph.py(STEPS 原在已删的 `workflow_runner`);`_STEP_OUTPUT_KEYS` 独立维护。L2 把 STEPS 迁到 `graph.py` 但没顺手统一另两处。
- **修复方向**:单一来源 = `agent.graph.STEPS`。`workflow_store.clear_outputs_after` 改为 import `STEPS`(或接收 `order` 参数默认 `STEPS`)。`_STEP_OUTPUT_KEYS` 保持 dict(它带 per-step 产出键),但加一个断言/测试 `list(_STEP_OUTPUT_KEYS.keys()) == STEPS` 防漂移。
- **涉及**:`agent/graph.py`、`workflow_store.py`、`workflow_executor.py` + 对应测试。

### F3. ⚠️决策点 — PUT/PATCH 未持 executor guard(并发 PUT+advance 可能丢更新)
- **表现**:`PUT /steps/{step}`(`executor.update_step` → `graph.update_state(as_node=...)`)与 `PATCH /specs`(`executor.update_spec` → `graph.update_state(specs dict-merge)`)都**不持** per-workflow `guard` 锁。若用户在 workflow 正 advance(worker 跑 `graph.stream`)时 PUT/PATCH,`graph.update_state` 与 `graph.stream` 在同一 `thread_id` 上并发。SqliteSaver 内部 lock 防**数据库损坏**,但 `update_state` 写入的 graph state 可能被 stream 结束时的 checkpoint 写覆盖(语义丢失,非崩溃)。旧 store-based PUT 也无锁(非回归)。
- **根因判断**:L2 设计保留旧 PUT 语义(不阻塞 running);但 langgraph 的 `update_state` vs `stream` 并发比旧 store 的 SQL 行级 upsert 更敏感(checkpoint 是 graph 级状态机位置 + channel values)。
- **修复方向(需与用户定)**:
  - (a) PUT/PATCH 走 `guard.acquire(blocking=False)`,失败→409(改行为,但合理 —— "running 时不能编辑";算契约修正,旧路径其实也该如此);
  - (b) blocking acquire 等 worker 结束(慢,可能超时);
  - (c) 接受现状 + 文档化(若 spike 证明不会真丢更新)。
  - **建议先 spike**:构造"advance 进行中 + PUT"的并发测试,观察是否真能丢更新;依结果选 (a)/(c)。
- **涉及**:`workflow_executor.py`(`update_step`/`update_spec`)、`api/routes/workflow.py`(若 409)、测试。

### F4. chromadb TOCTOU flake(pre-existing,L1 就有)
- **表现**:`tests/test_workflow_api.py::test_list_and_delete` ~20% 概率失败,chromadb `NotFoundError`(某 collection 不存在)。单个重跑通常过。全套件跑(线程多、压力大)时更易复现。
- **根因判断(L1 ledger 诊断,需复核)**:`IndexSupervisor.stop()` **不 join** worker → lifespan teardown 时 collection 还在被后台操作;stats 查询 / `_reset_collection` 之间存在 TOCTOU(查到 collection 名后、真正操作前 collection 被后台 drop)。**与 langgraph 无关**,是检索层老问题。
- **修复方向**:`stop()` join worker;stats/ensure 对 `NotFoundError` 重试或 tolerate(查不到当未建)。先复现 + 加日志定位确切的竞争窗口。
- **涉及**:`index_supervisor.py`、`vector_store.py`/chroma 适配层。

### F5. ⚠️决策点 — 测试环境 `.env` 隔离缺口(conftest 挡不住 pydantic 读 .env 文件)
- **表现**:`backend/.env` 含真实 `LANGCHAIN_API_KEY` 时,~10 个"无 key 降级"测试失败:
  `test_llm_modern::test_disabled_client_returns_none`(断言 `c.enabled is False` 但实为 `True`)、`test_llm_langchain::test_build_client_disabled_without_key`、`test_llm_modern::test_complete_document_returns_none_when_native_client_disabled`、`test_settings_llm::test_llm_disabled_without_langchain_api_key`、`test_intent::test_intent_disabled_llm_falls_back_to_rules`、`test_supervisor::test_health_local_embedding_ok_and_no_key_unknown`、`test_workflow_api::test_workflow_checkpoint_flow`/`test_put_step_overwrites_and_resets_status`/`test_advance_past_first_checkpoint_reaches_identify`/`test_document_capabilities_and_upload_validation`。
  `.env` 清空 key 时全绿。`.env` gitignored → main 上同样失败(非 L2 引入)。
- **根因判断**:pydantic-settings 的 `Settings` 配了 `env_file = BACKEND_DIR / ".env"`(`settings.py:14`),**直读文件**。conftest 的 `_isolate_llm_env` autouse fixture 只 `monkeypatch.delenv(key)`(清 `os.environ`),而 monkeypatch **无法撤销文件**。故 `.env` 有 key 时,`Settings(kb_dirs=..., data_dir=..., log_dir=...)` 即使不传 `api_key` 也会从文件读到 → `enabled=True`。
- **修复方向(需与用户定)**:让**测试态** Settings 不读 `.env`。选项:
  - (a) conftest 给 `Settings` 注入 `_env_file=None`(pydantic-settings 实例级覆盖)—— 但 fixture 得作用到每个 `Settings()` 构造,需 monkeypatch 类或 `__init__`;
  - (b) `Settings` 读 `.env.test`(测试态)而非 `.env`,conftest/pytest 指定;
  - (c) 把"无 key 降级"测试显式构造 `Settings(_env_file=None, ...)`(改测试,不动 conftest)。
  - **建议先 spike** pydantic-settings 的 `env_file` 覆盖优先级(init kwarg vs class config vs env)。最干净的大概是 (a) 或 (b)。
- **涉及**:`tests/conftest.py`(`_isolate_llm_env`)、`settings.py`,可能若干测试构造。

## 流程建议
1. 5 条独立,可分 5 个 commit。先开 `feat/langgraph-followups-cleanup`。
2. **带 ⚠️决策点的 F3 / F5 先和用户对齐方案**(可能需 brainstorm),其余 F1/F2/F4 可直接 TDD。
3. F1(serde API)、F3(并发)、F5(pydantic env_file 优先级)有未知 → 各自先 spike 再实现。
4. 用 superpowers:subagent-driven-development 或 executing-plans 执行;每条 followup 自审 + review。
5. 收尾跑全量测试(.env neutralize 后应全绿,followup 5 修了就不用 neutralize)+ ruff + finishing-a-development-branch。

## 验证标准(全部 followup 做完)
- `LANGGRAPH_STRICT_MSGPACK=true pytest` 无 unregistered warning、不报错(F1)。
- `grep -rn 'STEPS\|order = \["retrieve"'` 确认 STEPS 单一来源(F2)。
- PUT/PATCH 并发有明确语义(409 或文档化接受)(F3)。
- `test_list_and_delete` 连跑 10×0 flake(F4)。
- `.env` 含真实 key 时全量测试全绿(F5)。
- 全量绿 + ruff clean(L2 引入代码)+ `.env` 还原。
