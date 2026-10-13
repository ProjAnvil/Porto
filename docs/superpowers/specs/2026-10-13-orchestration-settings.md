# Spec: Langchain 编排开放配置（Orchestration Settings）

日期：2026-10-13 ｜ 状态：待实现 ｜ 负责人：Porto 团队

## 1. 背景与目标

Porto 的 Langchain 聊天管道从请求到回答共 14 层，其中编排相关配置（意图路由、检索前变换、重排、记忆、上下文预算）目前散落在「检索优化」「Agent」两个设置页，且意图分类器实现（LLM 优先 + 规则回退）硬编码、不可替换。

本特性目标：

1. **产品化开放编排配置**：新增「编排」设置页，按管道阶段组织配置，仅在 Langchain 引擎下可用。
2. **意图引擎可插拔**：意图路由层支持三类引擎——内置 LLM（默认，行为不变）/ 规则 / TypeSafe 外部决策引擎（System One 分类模型），含置信度阈值与低置信回退。
3. **设置页 sidebar 重组**：从 7 个扁平 section 重组为 4 个真实分组。
4. **依赖升级**：langchain 系列、claude-agent-sdk、anthropic 升级到当前最新。
5. **Playwright E2E**：从零搭建前端 E2E，验证设置页交互。

## 2. 范围

**MVP（本 spec 覆盖）**
- 后端：意图引擎注册表 + typesafe 引擎 + 新设置键 + 意图测试端点 + 响应元数据（engine/confidence/fallback）
- 前端：sidebar 分组重组 + 编排页（管道总览 + 5 张阶段卡 + agent_sdk 锁定态）
- 依赖升级 + 全量后端测试
- Playwright E2E 冒烟套件

**不做（二期）**
- `facts_enabled` 等 env-only 键转 UI 可配
- 编排层可视化编辑器、场景化预设
- 决策来源/回退率的统计分析面板（MVP 仅记日志 + 响应元数据）
- API key 读取脱敏（`api_key_set`/`tail` 模式）——现有各 `*_api_key` 均随 GET 返回明文，typesafe 保持一致，统一脱敏另行立项
- AgentSDK 路径的意图预路由（该路径刻意由 Claude 自主决策）

## 3. 产品决策（来自 PM 评审）

### 3.1 Sidebar IA（新）

```
对话引擎
  └ Agent              （引擎选择 + 模型配置，现"Agent"节，内容裁剪见 3.3）
编排 ★新增，仅 Langchain
  └ 编排               （管道总览 + 意图路由/检索前变换/检索/重排/记忆与预算）
知识检索
  ├ RAG               （chunk/top_k/kb_dirs/检索算法/embedding）
  ├ 检索优化           （Workflow 场景 query transform；chat 场景移入编排）
  └ 向量数据库
运行环境
  ├ 文件解析  ├ Knowledge  └ 架构
```

组头为不可点击的分隔标题。原「检索优化」「向量数据库」的缩进伪嵌套取消。

### 3.2 Langchain-only 策略：**置灰 + 顶部横幅，不隐藏**

agent_sdk 选中时编排页整页禁用（控件 disabled、值 50% 透明仍可辨认、保存按钮禁用），顶部 amber 横幅：「编排仅适用于 Langchain 聊天管道。当前 Chatbot 引擎为 Claude Agent SDK，此页配置暂不生效，已保存的配置会被完整保留。」+「前往 Agent 设置」链接按钮（唯一可交互元素）。切回 langchain 时以 `key={chatbotBackend}` 重挂载，draft 从最新配置重建。

### 3.3 配置迁移：纯 UI 重组，零数据迁移

存储 namespace 与键名不变，仅调整 UI 归属：
- 意图路由模式、chat 场景变换、condense、multi_query/hyde 参数：`rag_chat` → 从「检索优化」移入编排页
- 重排配置：`rag` namespace → 从「检索优化」移入编排页（键不动）
- `memory_compact_threshold`、`memory_recent_keep`、`context_char_budget`：`agent` namespace → 从「Agent」表单移入编排页（键不动）
- 编排页对各 namespace 走**部分更新**（PUT 只带本卡字段），依赖 ConfigStore merge-then-save 语义，不会清掉同 namespace 其他键

### 3.4 意图引擎产品形态

- 命名「意图引擎」，与「路由模式」（off/binary/adaptive）正交
- 默认 `builtin_llm`，与现行为完全一致（零行为变化上线）
- TypeSafe 引擎配置：API Key、Base URL（留空用 SDK 默认）、超时 ms、置信度阈值（slider+number 联动）、低置信自动回退开关（默认开，回退到内置 LLM）
- 「发送测试消息」按钮：实时显示意图、置信度、引擎、耗时
- 决策来源（engine / fallback / confidence）进入 ChatResponse.steps 元数据与日志

## 4. UX 设计（来自 UIUX 评审，要点）

- **管道总览**：横向 stepper 节点链（6 节点：①意图路由 ②检索前变换 ③检索 ④重排 ⑤记忆/Facts ⑥Prompt 预算），每节点带当前值摘要 chip + 脏点（rose 4px）；点击平滑滚动到对应卡片并 ring 高亮 800ms；窄屏转垂直紧凑列表
- **卡片**：复用 `SettingsCard`，扩展序号 `01–05` + 锚点 + 整卡锁定态；卡边界=保存单元边界
- **意图路由卡**三分区：路由模式（StrategyCardGroup）→ 路由引擎（三列 EngineCardGroup）→ TypeSafe 面板（选中才 inline 展开，`border-l-2 bg-zinc-50` 缩进样式；API Key 用 password input + Eye 切换；置信度 slider 0–1 step 0.05 + number 联动）
- **层级依赖**：路由模式 off 时引擎区禁用 + 提示「路由已关闭，无需选择引擎」
- **校验**：typesafe 选中但 API Key 为空 → 保存不禁用按钮，点击后字段级报错（border-rose-300 + 文案）；已配置后留空保存 = 保持不变
- **可访问性**：总览节点 button + aria-label；slider aria-valuetext；focus-visible 统一补齐；reduced-motion 时滚动/动画降级
- **agent_sdk 锁定态**：见 3.2，横幅 `role="note"`

## 5. 技术设计

### 5.1 后端：意图引擎

`models/enums.py` 新增：

```python
class IntentEngine(StrEnum):
    BUILTIN_LLM = "builtin_llm"
    RULE = "rule"
    TYPESAFE = "typesafe"
```

`intent.py` 重构（模块不拆，保持单一调用点）：

```python
@dataclass(frozen=True)
class IntentDecision:
    intent: ChatIntent
    reason: str
    confidence: float | None = None
    engine: str = ""          # builtin_llm / rule / typesafe / ""(off)
    fallback_from: str | None = None  # typesafe 降级时记录来源引擎
```

`route_chat_intent(message, settings, llm, routing_mode, engine=None)`，`engine` 缺省取 `settings.chat_intent_engine`：

- `off` → `RAG/routing_off`（现行为）
- `rule` → `_rule_route`（现函数），engine="rule"
- `builtin_llm`（默认）→ 现 LLM 优先 + 规则回退，engine 按实际胜者
- `typesafe` → `_typesafe_route`：
  - lazy import `typesafe_sdk`（依赖缺失/未配 key/异常/超时 → 按 `builtin_llm` 流程降级，`fallback_from="typesafe"`，reason 前缀 `typesafe:fallback:`）
  - `Choice(instructions=中文分类说明, criteria={mode 裁剪后的 intent: None})`，state 传 `{"message": message[:500]}`
  - 低置信（confidence < `chat_typesafe_confidence_threshold`）且开回退 → builtin_llm 流程，`fallback_from="typesafe"`；关回退 → 直接采用低置信结果
- 全部路径经 `logger.info("chat intent routed ...")` 记录 engine/confidence/fallback

调用点（仅 Langchain 路径两处）：`agent/orchestrator.py:_route_intent` 与 `agent/langchain_chat_stream.py` 的 `stream_chat`，读取 `effective_rag_chat_settings().intent_engine` 传入；steps 元数据 data 增加 `engine`/`confidence`/`fallback`。

### 5.2 后端：设置键（namespace `rag_chat`，全部新键）

| 键 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `chat_intent_engine` | IntentEngine | `builtin_llm` | — |
| `chat_typesafe_api_key` | str \| None | None | 敏感键 |
| `chat_typesafe_base_url` | str \| None | None | 留空=SDK 默认 |
| `chat_typesafe_timeout_ms` | int | 2000 | 100–30000 |
| `chat_typesafe_confidence_threshold` | float | 0.6 | 0.0–1.0 |
| `chat_typesafe_low_confidence_fallback` | bool | True | — |

同步点：`settings.py`（env 默认）、`RagChatSettingsPayload`（`| None = None` + 同约束）、`RAG_CHAT_SETTING_KEYS`、`default_rag_chat_settings()`、`SENSITIVE_SETTING_KEYS += "chat_typesafe_api_key"`。

### 5.3 后端：测试端点

`POST /api/intent/test`（新 `api/routes/intent.py`，挂载进 `app.py`）：

```jsonc
// 请求
{ "message": "你好", "routing_mode": "binary", "engine": "rule" }  // mode/engine 可省，省略取生效配置
// 响应
{ "intent": "direct", "reason": "greeting", "engine": "rule",
  "confidence": null, "fallback": false, "latency_ms": 1 }
```

不落库、无副作用；rule 引擎零外部依赖（E2E 用）。

### 5.4 依赖升级

| 包 | 现锁定 | 目标（2026-10-13 PyPI） |
|---|---|---|
| langchain-core | 1.5.1 | 1.6.10 |
| langchain-anthropic | 1.5.1 | 1.7.6 |
| langchain-openai | 1.4.1 | 1.7.1 |
| langchain-text-splitters | 1.1.2 | 1.1.3 |
| langgraph | 1.2.9 | 1.2.15 |
| langgraph-checkpoint-sqlite | 3.1.x | 3.1.2 |
| claude-agent-sdk | 0.2.128 | 0.2.165 |
| anthropic | 0.116.0 | 1.13.0（major） |
| typesafe-sdk（新增） | — | >=0.7.4 |

策略：`uv lock --upgrade-package <pkg>` 逐组升级 → `uv run pytest` 全量回归 → 绿后抬 pyproject 下限。anthropic 跨 major，若 breaking 则单独评估并记录。`langchain-typesafe`（0.0.1a3 alpha）**不引入**，直接依赖官方 `typesafe-sdk`。

### 5.5 前端

- `types.ts`：`RagChatConfig` 增 5.2 键；`api.ts` 默认值镜像
- sidebar：静态数组 → 分组渲染（组头 + section 按钮），新增 `orchestration`
- `OrchestrationPage`（porto-workbench.tsx 内新增组件群，沿用现有模式）：
  - `PipelineOverviewRail`（6 节点 + 摘要 chip + 脏点 + 滚动联动）
  - 卡 1 意图路由（含 typesafe 条件面板 + `POST /api/intent/test` 测试按钮）
  - 卡 2 检索前变换（自 RagOptimizationSettingsForm 迁入 chat 部分）
  - 卡 3 检索（retrieval_method + hybrid_vector_weight，rag namespace 部分保存）
  - 卡 4 重排（自 RagOptimizationSettingsForm 迁入）
  - 卡 5 记忆与上下文预算（自 AgentSettingsForm 迁入 3 键）
  - 锁定态：`chatbot_backend === "agent_sdk"` 时整页禁用 + 横幅
- 「检索优化」节保留 workflow 场景变换；「Agent」节移除记忆 3 键

### 5.6 E2E（Playwright，从零搭建）

- `frontend/playwright.config.ts`：两个 webServer（backend `uvicorn` :8100 / frontend `next dev` :3100），baseURL :3100
- 用例（`frontend/e2e/orchestration.spec.ts`）：
  1. 设置页分组 sidebar 渲染 + 导航到编排
  2. 意图引擎选「规则」→ 保存 → 刷新持久化
  3. 切 TypeSafe → 面板展开；空 API Key 保存 → 字段级报错
  4. 测试按钮（rule 引擎）→ 返回 direct/greeting 决策
  5. Agent 节切 agent_sdk → 编排页锁定横幅 + 控件禁用；切回恢复
- 关键交互元素加 `data-testid`

## 6. 验收标准

1. `uv run pytest`（backend）全绿；升级后无 langchain/anthropic 弃用告警阻塞
2. 默认配置（builtin_llm）下聊天行为与升级前一致（意图路由测试快照不变）
3. 编排页在 langchain 模式可配置 5 张卡并正确部分保存（不破坏同 namespace 其他键）
4. agent_sdk 模式下编排页锁定且已存配置不被清空
5. typesafe 引擎：未配 key 自动降级 builtin_llm 且日志/元数据可见；配 key 后按置信度决策/回退
6. Playwright 套件全绿
7. 老用户（已有 rag_chat/rag/agent 存量配置）升级后配置无感保留

## 7. 风险

| 风险 | 缓解 |
|---|---|
| anthropic 1.x breaking | 升级独立成任务，测试全量回归；失败则回滚该包并记录 |
| langchain-core 1.6 与 llama-index 系兼容 | uv 解析 + 全量测试 |
| typesafe-sdk API 变动（0.x） | lazy import + 降级路径；锁定 >=0.7.4 |
| 前端 4610 行单文件继续膨胀 | 编排组件群集中在文件尾部新区块；二期拆文件 |
| TypeSafe 中文分类质量 | 测试端点 + 日志可观测；低置信回退默认开 |
