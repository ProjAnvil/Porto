# 后端反模式消除设计

> 日期：2026-08-05  
> 状态：Draft → 待审计  
> 分支：refactor/enum-migration（接续枚举化重构）

## 1. 目标

消除后端 Python 代码中的工程反模式，参考 [8 Python Anti-Patterns That Break Code in 2026](https://blog.devgenius.io/8-python-anti-patterns-that-break-code-in-2026-d72410fe9928) 和 [18 Common Python Anti-Patterns](https://medium.com/data-science/18-common-python-anti-patterns-i-wish-i-had-known-before-44d983805f0f)。

### 范围

| 反模式 | 来源 | 纳入 |
|--------|------|------|
| 1. dict 返回值（应该用结构体） | Anti-pattern #2 | ✅ |
| 2. 裸 `except Exception:` 无日志 | Anti-pattern #5 | ✅ |
| 3. 魔法数字 | Anti-pattern #1 | ✅ |
| 4. 过长函数（God method） | Anti-pattern #7 | ✅ |

### 不做的事

- 不改 LangGraph state 返回值（`{"steps": [...]}` 是框架要求的 dict 格式）
- 不改 JSON schema 生成器返回的 dict（`_critique_schema()`, `subsystem_schema()` — 给 LLM 的 schema 必须是 dict）
- 不改数据库查询结果的 dict（动态映射，key 不固定）
- 不改 MCP 工具文本包装（协议要求的 dict 格式）

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
- 函数内部的 dict 构造改为 model 构造
- `checks` 列表中的 dict 改为 `WorkflowCheck` 实例
- `metrics` dict 改为 `RagMetrics` 实例

**改动 `models/__init__.py`：**
- 导出新增的 evaluation models

**改动调用方：**
- `workflow_executor.py` — `evaluation` 字段赋值处（model 自动序列化为 dict）
- `api/routes/eval.py` — 返回 evaluation 结果处
- 注意：Pydantic model 嵌入 `dict[str, Any]` 字段（如 `WorkflowResponse.evaluation`）时会自动 `.model_dump()`，行为不变

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
- 函数内部 dict 构造改为 dataclass 构造

**改动调用方：**
- `agent/langchain_chat.py` — 消费 `get_summary()` 返回值的地方（`summary["summary"]` → `summary.summary`）
- `agent_sdk/backend.py` — 如有消费

## 3. 反模式 2：裸 except Exception 加日志

以下 `except Exception:` 无 `as exc` binding 的位置，需要加 `as exc` + `logger.warning` 或 `logger.debug`（视上下文）：

| 文件:行 | 上下文 | 修复方式 |
|---------|--------|----------|
| `agent_sdk/backend.py:97` | API key 设置 | 加 `as exc` + `logger.warning` |
| `agent_sdk/backend.py:122` | options 构建 | 加 `as exc` + `logger.warning` |
| `agent_sdk/backend.py:356` | resume 会话 | 加 `as exc` + `logger.warning` |
| `llm/client.py:151` | JSON 解析 | 加 `as exc` + `logger.debug`（解析失败是正常的） |
| `llm/client.py:365` | document 完成 | 加 `as exc` + `logger.warning` |
| `memory/store.py:257` | collection reset | 加 `as exc` + `logger.debug`（collection 不存在是正常的） |
| `memory/facts.py:217` | facts 提取 | 加 `as exc` + `logger.warning` |
| `vector_store.py:104` | chunk 添加 | 加 `as exc` + `logger.warning` |
| `vector_store.py:167` | delete collection | 加 `as exc` + `logger.debug` |
| `vector_store.py:311` | BM25 构建 | 加 `as exc` + `logger.warning` |
| `vector_store.py:333` | reindex lock | 加 `as exc` + `logger.warning` |
| `vector_store.py:341` | collection metadata | 加 `as exc` + `logger.warning` |
| `health.py:76` | probe cycle | 加 `as exc` + `logger.exception`（已有 logger.exception） |

**注意**：`health.py:76` 已经有 `logger.exception`，只是没有 `as exc` binding。这里不需要改——`logger.exception` 不需要 binding 就能记录 traceback。

## 4. 反模式 3：魔法数字提取为常量

### 4.1 截断/长度限制

| 文件:行 | 当前值 | 常量名 |
|---------|--------|--------|
| `intent.py:66` | `message[:500]` | `_MAX_INTENT_MESSAGE_CHARS = 500` |
| `llm/client.py:332` | `full[-200:]` | `_CONTINUATION_TAIL_CHARS = 200` |
| `vector_store.py:119` | `chunk.text[:120]` | `_CHUNK_ID_PREVIEW_LEN = 120` |
| `specs/loop.py:60` | `feedback[:200]` | `_FEEDBACK_DIGEST_MAX = 200` |
| `heuristics.py:22` | `[:180]` | `_SUMMARY_MAX_CHARS = 180` |
| `langchain_chat.py:213,392` | `s.text[:180]` | `_SOURCE_PREVIEW_CHARS = 180` |
| `memory/store.py:102` | `limit: int = 500` | `_DEFAULT_MESSAGE_FETCH_LIMIT = 500` |

### 4.2 评估阈值

| 文件:行 | 当前值 | 常量名 |
|---------|--------|--------|
| `evaluation.py:27` | `>= 120` | `_MIN_UNDERSTANDING_CHARS = 120` |
| `evaluation.py:57` | `>= 55` | `_RAG_PASS_SCORE = 55` |
| `evaluation.py:82` | `>= 55` | 复用 `_RAG_PASS_SCORE` |
| `evaluation.py:51-54` | `0.30, 0.25, 0.25, 0.20` | `_RAG_WEIGHT_*` 或保持（权重数学公式，内联可接受） |

### 4.3 其他魔法数字

| 文件:行 | 当前值 | 常量名 |
|---------|--------|--------|
| `evaluation.py:83` | score weights `20` | `_WORKFLOW_CHECK_WEIGHT = 20`（或保持，5 个 check 各 20 分，可接受内联） |
| `health.py:221` | `limit: int = 200` | `_EXC_DETAIL_LIMIT = 200` |

**注意**：score 权重（如 0.30/0.25/0.25/0.20）和 check 分值（20）属于评估算法的数学参数，内联在计算公式旁是可接受的——它们不是"无意义的魔法值"，而是公式的一部分。但如果审计认为应该提取，则提取。

## 5. 反模式 4：过长函数拆分

### 5.1 agent_sdk/backend.py — `_build_chat_options`（~376 行区域）

该区域包含 `_build_chat_options` 和可能紧随其后的 `_chat` / `_chat_stream` 方法。

**拆分策略：**
- 将 `_build_chat_options` 中的 MCP server 配置、tools 构建、env 设置拆为独立私有方法
- 将 `_chat` / `_chat_stream` 中的消息处理循环、session ID 捕获、结果组装拆为独立方法
- 每个子方法控制在 30-50 行以内，单一职责

**实施约束：**
- 保持函数签名和公共接口不变
- 拆分后的子方法以 `_` 前缀命名
- 不改变任何运行时行为

### 5.2 agent/langchain_chat.py — `langchain_chat`（~340 行）

该函数包含完整的同步 chat 流程：意图路由 → RAG 检索 → 回复生成。

**拆分策略：**
- 提取 `_handle_direct_chat(req, settings)` — 寒暄/闲聊路径
- 提取 `_handle_rag_chat(req, settings)` — RAG 检索 + 回复路径
- 提取 `_build_chat_response(...)` — 组装 ChatResponse
- `langchain_chat` 函数体缩减为路由分发 + 调用上述方法

**实施约束：**
- 保持 `langchain_chat` 的公共签名不变
- 子函数返回中间结果，由 `langchain_chat` 组装最终 `ChatResponse`
- 不改变任何运行时行为

## 6. 受影响文件清单

| 文件 | 改动类型 |
|------|----------|
| `models/evaluation.py` | **新建** — evaluation Pydantic models |
| `models/__init__.py` | 导出 evaluation models |
| `evaluation.py` | dict 返回值 → model，魔法数字提取 |
| `memory/store.py` | get_summary dict → dataclass，魔法数字 |
| `agent_sdk/backend.py` | 裸 except 加日志，函数拆分 |
| `agent/langchain_chat.py` | 函数拆分，get_summary 消费适配，魔法数字 |
| `llm/client.py` | 裸 except 加日志，魔法数字 |
| `vector_store.py` | 裸 except 加日志，魔法数字 |
| `memory/facts.py` | 裸 except 加日志 |
| `health.py` | 魔法数字 |
| `intent.py` | 魔法数字 |
| `specs/loop.py` | 魔法数字 |
| `agent/heuristics.py` | 魔法数字 |
| `workflow_executor.py` | evaluation 消费适配 |

## 7. 兼容性保障

- Pydantic model 嵌入 `dict[str, Any]` 字段时自动序列化为 dict，API JSON 输出不变
- dataclass 返回值在调用方改为属性访问（`.summary` 代替 `["summary"]`）
- 裸 except 加日志不影响异常处理逻辑（仍然 catch + 继续）
- 魔法数字提取为常量不影响运行时值
- 函数拆分不改变公共接口和运行时行为

## 8. 测试策略

全部 324 个现有测试必须通过。重点关注：
- `test_evaluation.py` — evaluation 返回值结构变化
- `test_chat_dispatch.py` / `test_agent_sdk_chat.py` — chat 流程
- `test_workflow_api.py` / `test_workflow_executor.py` — workflow evaluation 消费
- `test_llm_*` — LLM client 异常处理
- `test_vector_store.py` — vector store 异常处理
