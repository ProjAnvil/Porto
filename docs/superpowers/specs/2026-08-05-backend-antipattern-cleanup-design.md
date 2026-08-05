# 后端反模式消除设计（v2 — 审计修正版）

> 日期：2026-08-05  
> 状态：审计通过，待实施  
> 分支：refactor/enum-migration

## 1. 目标

消除后端 Python 代码中的工程反模式，参考 [8 Python Anti-Patterns That Break Code in 2026](https://blog.devgenius.io/8-python-anti-patterns-that-break-code-in-2026-d72410fe9928)。

### 范围

| 反模式 | 纳入 |
|--------|------|
| 1. dict 返回值 → 结构化类型 | ✅ |
| 2. 静默 `except Exception:` 补日志 | ✅ |
| 3. 魔法数字提取为常量 | ✅ |
| 4. 过长函数拆分 | ✅ |

### 不做的事

- 不改 LangGraph state 返回值（框架要求的 dict）
- 不改 JSON schema 生成器返回的 dict（给 LLM 的 schema 必须 dict）
- 不改数据库查询结果的 dict（动态映射）
- 不改 MCP 工具文本包装（协议要求）

## 2. 反模式 1：dict 返回值 → 结构化类型

### 2.1 evaluation.py（3 个函数 → Pydantic model）

**新建 `models/evaluation.py`：**

```python
from pydantic import BaseModel

class WorkflowCheck(BaseModel):
    name: str
    passed: bool
    weight: int

class WorkflowEvaluation(BaseModel):
    score: int
    passed: bool
    checks: list[WorkflowCheck]
    prd_chars: int
    spec_rubric_avg: float | None = None    # evaluate 节点动态注入
    spec_rubric_min: float | None = None    # evaluate 节点动态注入

class RagMetrics(BaseModel):
    answer_relevance: float
    context_relevance: float
    groundedness: float
    faithfulness: float

class RagCaseEvaluation(BaseModel):
    question: str
    score: float
    passed: bool
    metrics: RagMetrics
    notes: str

class RagBatchEvaluation(BaseModel):
    score: float
    passed: bool
    cases: list[RagCaseEvaluation]
```

**改动 `evaluation.py`：**
- `evaluate_workflow() -> dict` → `-> WorkflowEvaluation`
- `evaluate_rag_case() -> dict` → `-> RagCaseEvaluation`
- `evaluate_rag_cases() -> dict` → `-> RagBatchEvaluation`
- 函数内部 dict 构造改为 model 构造
- `checks` 列表中 dict → `WorkflowCheck` 实例
- `result["score"]` 等 dict 访问 → `result.score` 属性访问
- `c["weight"]`、`c["passed"]` → `c.weight`、`c.passed`
- `r["score"]` → `r.score`

**改动 `models/__init__.py`：**
- 导出新增 evaluation models

**Pydantic dict[str, Any] 兼容方案（显式 .model_dump()）：**

所有将 evaluation model 赋值给 `dict[str, Any]` 字段的调用点，必须显式 `.model_dump()`：

| 调用点 | 当前代码 | 改后 |
|--------|---------|------|
| `agent/langchain_chat.py` ~行220 | `ChatResponse(evaluation=evaluate_rag_cases(...))` | `ChatResponse(evaluation=evaluate_rag_cases(...).model_dump())` |
| `agent/langchain_chat.py` ~行405 | 同上（stream 路径） | 同上 |
| `agent_sdk/backend.py` ~行481 | `ChatResponse(evaluation=evaluate_rag_cases(...))` | `.model_dump()` |
| `agent_sdk/backend.py` ~行629 | 同上（stream 路径） | 同上 |
| `workflow_executor.py` 投影路径 | `evaluation=evaluate_workflow(...)` | `.model_dump()` |

**dict 风格读取改为属性访问（全部清单）：**

| 文件:行 | 当前 | 改后 |
|---------|------|------|
| `evaluation.py:51` | `c["weight"]`、`c["passed"]` | `c.weight`、`c.passed` |
| `evaluation.py:54` | `result["score"]`、`result["passed"]` | `result.score`、`result.passed` |
| `evaluation.py:58,103,110,112` | `result["score"]`、`r["score"]` | `result.score`、`r.score` |
| `agent/langchain_chat.py:234,270` | `evaluation["score"]` | `evaluation["score"]`（evaluation 此时是 dict，不改） |
| `agent_sdk/backend.py:500,640` | `evaluation['score']` | 同上（dict，不改） |
| `agent/nodes/evaluate.py:19-20` | `evaluation["spec_rubric_avg"] = ...` | `evaluation.spec_rubric_avg = ...`（改为属性赋值） |
| `agent/nodes/evaluate.py:25,27,36,37,46` | `evaluation.get(...)` / `evaluation['score']` | `getattr(evaluation, ...)` 或属性访问 |
| `tests/test_evaluation.py:20,21` | `result["score"]`、`result["passed"]` | `result.score`、`result.passed` |

**注意**：`agent/nodes/evaluate.py` 接收的 `evaluation` 来自 `evaluate_workflow()` 返回值，经 LangGraph state 传递。改为 model 后，state 中存储的是 model 实例（如果 Pydantic 序列化为 dict 存入 state，则 evaluate 节点收到的仍是 dict）。需要验证 LangGraph state 的序列化行为——如果 state 中 evaluation 已被序列化为 dict，则 evaluate 节点仍用 dict 访问，无需改。

**重要**：`langchain_chat.py` 和 `agent_sdk/backend.py` 中的 `evaluation['score']` 访问的是 `ChatResponse.evaluation` 字段（类型 `dict[str, Any]`），在调用点做了 `.model_dump()` 后它就是 dict，**不需要改这些访问点**。

### 2.2 memory/store.py（get_summary → dataclass）

**改动 `memory/store.py`：**
- 新增 dataclass：
```python
@dataclass
class SessionSummary:
    summary: str
    last_message_id: str
    created_at: str
```
- `get_summary() -> dict | None` → `-> SessionSummary | None`

**改动消费方 `memory/compaction.py:61-67`（唯一调用点）：**
- `cached.get("last_message_id")` → `cached.last_message_id`
- `cached.get("summary")` → `cached.summary`
- `cached["summary"]` → `cached.summary`

**注意**：`langchain_chat.py` 和 `agent_sdk/backend.py` 不直接调用 `get_summary()`，它们调用的是 `get_compacted_history()`，后者返回的是字符串 summary，不受影响。

## 3. 反模式 2：静默 except Exception 补日志

### 3.1 真正的静默吞异常（无日志无 raise，5 处）

这些是最该修的——在测试 teardown 和崩溃恢复路径上静默吞异常：

| 文件:行 | 上下文 | 修复 |
|---------|--------|------|
| `api/deps.py:298` | `reset_rag_singletons` — supervisor.stop() | 加 `as exc` + `logger.warning` |
| `api/deps.py:302` | `reset_rag_singletons` — health.stop() | 加 `as exc` + `logger.warning` |
| `api/deps.py:318` | `reset_rag_singletons` — checkpoint conn.close() | 加 `as exc` + `logger.warning` |
| `workflow_executor.py:151` | `_worker` — update_status(FAILED) | 加 `as exc` + `logger.warning` |
| `workflow_executor.py:402` | `_worker_rerun` — update_status(FAILED) | 加 `as exc` + `logger.warning` |

### 3.2 已有 logger 但无 as exc binding（可选改进）

这些已有 `logger.exception` 或 `logger.info`，只是没有 `as exc` binding。属于代码风格统一，非反模式治理。**本次不改**（YAGNI——它们已经记录了 traceback）：

- `agent_sdk/backend.py:97` — sqlite 读取失败，已有上下文日志
- `agent_sdk/backend.py:122` — sqlite 写入失败，已有上下文日志
- `agent_sdk/backend.py:356` — 已有 `self.logger.exception`
- `llm/client.py:151` — 已有 `logger.exception` + `raise`
- `llm/client.py:365` — 已有 `logger.exception` + `raise`
- `memory/store.py:257` — collection reset，已有 `logger.info`
- `memory/facts.py:217` — 已有上下文日志
- `vector_store.py:104,167,311,333,341` — 已有 `logger.exception` 或 `logger.info`
- `health.py:76` — 已有 `logger.exception`

## 4. 反模式 3：魔法数字提取为常量

### 4.1 截断/长度限制

| 文件:行 | 当前值 | 常量名 |
|---------|--------|--------|
| `intent.py:66` | `message[:500]` | `_MAX_INTENT_MESSAGE_CHARS = 500` |
| `intent.py:81` | `reason[:80]` | `_MAX_REASON_CHARS = 80` |
| `intent.py:93` | `<= 12` | `_SHORT_MESSAGE_THRESHOLD = 12` |
| `llm/client.py:332` | `full[-200:]` | `_CONTINUATION_TAIL_CHARS = 200` |
| `vector_store.py:119` | `chunk.text[:120]` | `_CHUNK_ID_PREVIEW_LEN = 120` |
| `vector_store.py:134` | `>= 64` | `_EMBED_BATCH_SIZE = 64` |
| `specs/loop.py:60` | `feedback[:200]` | `_FEEDBACK_DIGEST_MAX = 200` |
| `heuristics.py:17` | `first[:40]` | `_MAX_PROJECT_NAME_CHARS = 40` |
| `heuristics.py:22` | `[:180]` | `_SUMMARY_MAX_CHARS = 180` |
| `heuristics.py:126-128` | `[:12]` ×3 | `_MAX_LIST_ITEMS = 12` |
| `langchain_chat.py:213,392` | `s.text[:180]` | `_SOURCE_PREVIEW_CHARS = 180` |
| `langchain_chat.py:214,393` | `sources[:4]` | `_MAX_FALLBACK_SOURCES = 4` |
| `langchain_chat.py:408` | `sources[:6]` | `_MAX_SSE_SOURCES = 6` |
| `memory/store.py:102` | `limit: int = 500` | `_DEFAULT_MESSAGE_FETCH_LIMIT = 500` |

### 4.2 评估阈值

| 文件:行 | 当前值 | 常量名 |
|---------|--------|--------|
| `evaluation.py:27` | `>= 120` | `_MIN_UNDERSTANDING_CHARS = 120` |
| `evaluation.py:57,82` | `>= 55` | `_RAG_PASS_SCORE = 55` |
| `evaluation.py:133` | `>= 0.35` | `_SENTENCE_SUPPORT_THRESHOLD = 0.35` |

**保持内联（公式参数，非魔法值）：**
- `evaluation.py:51-54` 权重 `0.30/0.25/0.25/0.20` — RAGAS 维度权重公式
- `evaluation.py:24-40` check 分值 `20` — 5 项各 20 分
- `health.py:54` `max_workers=3` — 线程池大小
- `workflow_executor.py:106,112` `30.0/0.05` — 超时/轮询间隔

## 5. 反模式 4：过长函数拆分

### 5.1 agent/langchain_chat.py — `langchain_chat_stream`（212 行）

**当前**：行 277-488，包含 SSE 编码 + 检索 + facts + 流式/非流式分支 + inspector 组装 + 异常处理。

**拆分策略：**
- 提取 `_route_chat_intent(req, settings)` — 意图路由（复用，`langchain_chat` 和 `langchain_chat_stream` 共用）
- 提取 `_handle_stream_direct(req, settings)` — 流式寒暄路径
- 提取 `_handle_stream_rag(req, settings)` — 流式 RAG 路径（SSE 编码 + 检索 + 流式回复）
- `langchain_chat_stream` 缩减为路由分发 + 调用上述方法

### 5.2 agent_sdk/backend.py — `chat_stream`（162 行）

**当前**：行 505+，包含 streaming 会话管理 + event 循环 + 结果组装。

**拆分策略：**
- 提取 `_consume_stream_events(...)` — event 循环处理
- 提取 `_assemble_stream_response(...)` — 结果组装
- `chat_stream` 缩减为会话初始化 + 调用上述方法

### 5.3 agent_sdk/backend.py — `chat`（115 行）

**当前**：行 388+，非流式 chat。

**拆分策略：**
- 提取 `_consume_chat_events(...)` — message 循环处理
- `chat` 缩减为初始化 + 调用

**实施约束（所有拆分）：**
- 保持函数签名和公共接口不变
- 子方法以 `_` 前缀命名
- 不改变任何运行时行为
- 每个子方法控制在 30-50 行以内

## 6. 受影响文件清单

| 文件 | 改动类型 |
|------|----------|
| `models/evaluation.py` | **新建** — evaluation Pydantic models |
| `models/__init__.py` | 导出 evaluation models |
| `evaluation.py` | dict→model + dict 访问→属性 + 魔法数字 |
| `memory/store.py` | get_summary dict→dataclass + 魔法数字 |
| `memory/compaction.py` | SessionSummary 消费适配 |
| `agent_sdk/backend.py` | 静默 except + 函数拆分(chat/chat_stream) + .model_dump() |
| `agent/langchain_chat.py` | 函数拆分(langchain_chat_stream) + .model_dump() + 魔法数字 |
| `agent/nodes/evaluate.py` | evaluation 动态扩展适配 |
| `workflow_executor.py` | 静默 except + .model_dump() + 魔法数字 |
| `api/deps.py` | 静默 except（3 处） |
| `api/routes/eval.py` | .model_dump()（如有） |
| `intent.py` | 魔法数字 |
| `vector_store.py` | 魔法数字 |
| `specs/loop.py` | 魔法数字 |
| `agent/heuristics.py` | 魔法数字 |
| `tests/test_evaluation.py` | dict 访问→属性 |

## 7. 兼容性保障

- **evaluation model → dict[str, Any]**：调用点显式 `.model_dump()`，API JSON 输出不变
- **SessionSummary**：消费方从 dict 访问改为属性访问
- **静默 except**：仅补加日志，不改变异常处理逻辑
- **魔法数字**：提取为常量不影响运行时值
- **函数拆分**：不改变公共接口和运行时行为

## 8. 测试策略

`pytest` 全绿。重点关注：
- `test_evaluation.py` — evaluation 返回值结构变化
- `tests/memory/test_compaction.py` — SessionSummary 消费
- `test_chat_dispatch.py` / `test_agent_sdk_chat.py` — chat/chat_stream 拆分
- `test_agent_graph.py` — evaluate 节点动态扩展
- `test_workflow_api.py` / `test_workflow_executor.py` — workflow evaluation 消费
- `test_intent.py` — 魔法数字提取后的阈值行为
- `test_node_backend_dispatch.py` — backend dispatch
