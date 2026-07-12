# TODO — Backend Agent 架构现代化（激进版 v2）

| 日期 | 2026-07-12 |
|------|------------|
| 关联 | [PLANs/2026-07-12-spec-refine-loop-and-agent-modernization.md](../PLANs/2026-07-12-spec-refine-loop-and-agent-modernization.md) |

> 路径均相对 `chatbot/backend/`。`[P0]` 必做，`[P1]` 强烈建议，`[P2]` 进阶。

---

## Phase 0 — LLM 基础设施 + 工具层 ✅ 已完成

- [x] **[P0]** `src/porto_chatbot/settings.py`：新增字段 `critic_*` / `spec_refine_*` / `workflow_rework_*` / `agent_stream_enabled` / `agent_max_tool_turns`
- [x] **[P0]** `src/porto_chatbot/llm.py`：`complete` 支持传入 `messages: list[dict]`，向后兼容现有 `system+user`
- [x] **[P0]** `src/porto_chatbot/llm.py`：新增 `complete_structured(system, user, schema) -> dict | None`（JSON 解析 + 1 次重试，失败返回 None）
- [x] **[P0]** `src/porto_chatbot/llm.py`：新增 `complete_with_tools(system, user, tools, max_turns) -> ToolLoopResult`（openai `tools=` / anthropic `tool_use`，自动执行 tool 并回填）
- [x] **[P0]** `src/porto_chatbot/llm.py`：新增 `stream(system, user) -> Iterator[str]`（native token stream；openai `stream=True` / anthropic stream events）
- [x] **[P0]** `src/porto_chatbot/tools.py`（新）：定义 `search_knowledgebase` / `get_prd_text` / `get_understanding` / `list_subsystems` / `get_subsystem` / `get_sources`，提供 openai 与 anthropic 两套 schema 适配
- [x] **[P0]** `tests/test_llm_modern.py`：messages 历史 / 结构化解析（成功+失败重试+返回 None）/ tool loop（1 轮+多轮+降级）/ stream 分片
- [x] **[P0]** `tests/test_tools.py`：每个 tool 的契约 + schema 适配

> Phase 0 验证：51 测试全绿（28 LLM + 13 tools + 10 原有），ruff 全过，零回归。

## Phase 1 — understand + identify 改 LLM 驱动 ✅ 已完成

- [x] **[P0]** `agent.py` `understand_prd`：改为 LLM `complete_with_tools` 驱动（可调 `get_prd_text`/`search_knowledgebase`）；正则路径降为 fallback
- [x] **[P0]** `agent.py` `identify_subsystems`：改为 LLM `complete_structured` 输出子系统列表（JSON schema）；`DOMAIN_HINTS` 降为 fallback
- [x] **[P1]** `agent.py` `_subsystem_schema()`：约束 LLM 输出结构
- [x] **[P0]** `tests/test_agent.py`：两节点的 LLM 正常 / 异常 / 降级三态（+normalize 鲁棒性）

> Phase 1 验证：56 测试全绿、ruff 全过、零回归。LLM 路径与 DOMAIN_HINTS 降级路径通过 mock 隔离测试。

## Phase 2 — Spec evaluator-optimizer loop ✅ 已完成

- [x] **[P0]** `src/porto_chatbot/specs.py`（新）：常量 `SPEC_RUBRIC`（6 维）
- [x] **[P0]** `models.py`：`Verdict` / `Critique` / `SpecAttempt` / `SpecResult`
- [x] **[P0]** `specs.py`：`generate_initial_spec` / `critique_spec`（结构化输出）/ `refine_spec` / `generate_spec_with_loop`（四重终止）
- [x] **[P0]** `agent.py` `generate_specs`：改用 loop，`spec_refine_parallel` 时并行，写 `AgentStep.data.attempts`
- [x] **[P0]** `tests/test_spec_loop.py`：PASS 即停 / max_iter 截断 / 分数不升回退 / critic 异常跳过 / generate 异常降级模板 / 预算截断
- [x] **[P1]** 预算上限触发 `truncated`（简易字符预算实现）
- [x] **[P1]** critic 走独立 `critic_*` 模型（SpecContext.critic_llm + PortoAgent._build_critic_llm，未配回退 generator）

> Phase 2 验证：69 测试全绿、ruff 全过、零回归。四重终止条件全部被测试覆盖；`_render_spec` 已下沉为 `specs.render_template_spec` 的降级路径。

## Phase 3 — Evaluate 语义化 + 条件回边 ✅ 已完成

- [x] **[P0]** `agent.py` `evaluate`：聚合 SpecResult 分数到 `evaluation.spec_rubric_avg` / `spec_rubric_min`
- [x] **[P0]** `agent.py` `_build_graph`：加 `evaluate → identify_subsystems` 条件回边（`workflow_rework_enabled` + `max_passes`）
- [x] **[P0]** `tests/test_agent.py`：回边触发 / 不触发 / max_passes=0 / disabled / 路由函数 / 端到端回边（identify×2）

> Phase 3 验证：76 测试全绿、ruff 全过、零回归。线性 DAG 已升级为带条件回边的 LangGraph：evaluate 不达标且未超 `workflow_rework_max_passes` 时回到 identify_subsystems 重做。降级路径（无 LLM）evaluate passed → 不回边，`len(steps)==5` 不变。

## Phase 4 — Memory compaction ✅ 已完成

- [x] **[P0]** `memory/` 包：会话超阈值触发 compaction（远期摘要 + 近期原文），按 last_message_id 缓存
- [x] **[P0]** `tests/test_memory_compaction.py`：超阈 compaction / 缓存命中 / 消息增长重摘要 / 无 LLM 降级（6 测试）
- [x] **[P1]** chat 路由 context 字符预算（`context_char_budget` 默认 16000 + `_trim_to_budget` 从后截断，chat/chat_stream 接入）

> Phase 4 验证：81 测试全绿。

## Phase 5 — Native streaming + Intent 升级 ✅ 已完成

- [x] **[P0]** `api/routes/chat.py` `/api/chat/stream`：RAG 分支改用 `LLMClient.stream` 原生 token → SSE `text-delta`（`agent_stream_enabled` 开关，disabled 降级 complete）
- [x] **[P1]** `intent.py`：升级为 LLM function-calling router（`complete_structured`），规则降级保留
- [x] **[P0]** `tests/test_intent.py`（5 测试）+ `tests/test_api.py` native streaming 集成测试

> Phase 5 验证：87 测试全绿、ruff 全过。

---

## 完成定义

- [x] 每阶段 `pytest` 全绿（96 测试）、ruff 全过
- [x] 无 API key 时降级路径行为与现状一致（零回归，降级路径全程覆盖）
- [x] spec rubric 均分对照基线显著提升（deepseek 实测：模板 3.20 → LLM loop 11.4 / 12，+8.20）
- [x] 结构化日志可还原全部 tool call / loop 迭代 / 回边
