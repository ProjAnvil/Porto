# 后端枚举化重构设计（v2 — 审计修正版）

> 日期：2026-08-05  
> 状态：审计通过，待实施

## 1. 目标

消除后端 Python 代码中的魔法字符串（string literal），使用 Python 3.12 `StrEnum` 替代 `Literal[...]` 类型别名和内联字符串比较，提升类型安全性和代码可维护性。

### 范围

| 类别 | 描述 | 纳入 |
|------|------|------|
| A. Literal 类型别名 | `Verdict = Literal["PASS","FAIL"]` 等 | ✅ |
| B. 内联 Literal | `Literal["openai","anthropic"]` 等 20+ 处 | ✅ |
| C. String 比较 | `if x == "anthropic":` 等 50+ 处 | ✅ |
| D. 外部 API 字符串 | `finish_reason`, `msg.subtype` 等 | ✅ |
| E. Dict 魔法 key | `state["answer_text"]` 等 LangGraph 状态 key | ❌ 不纳入 |

### 不做的事

- 不改 Pydantic ↔ dataclass 的架构边界
- 不改 LangGraph `TypedDict` 状态 key（E 类）
- 不改前端代码（StrEnum 的 JSON 序列化值与现有字符串完全一致）
- 不新增/删除功能，纯重构

### 显式排除的字符串（保留 str，不枚举化）

以下字符串虽为领域语义，但因**跨模块契约**或**动态拼接**性质，显式排除：

| 文件:行 | 字符串 | 排除原因 |
|---------|--------|----------|
| `agent/langchain_chat.py` decision.reason | `"greeting"`, `"smalltalk_or_help"`, `"llm:{...}"` | 自由文本 + 动态拼接 |
| `agent_sdk/backend.py` NodeExecutionResult.reason | `"agent_sdk_error"`, `msg.subtype` 透传 | 自由文本 + 动态拼接 |
| `index_supervisor.py` rag reason | `"reindexing"`, `"index_unavailable"` | 跨模块契约（被 `langchain_chat._RAG_UNAVAILABLE_HINTS` 消费） |
| `documents.py` DocumentArtifact.parser | `"text"`, `"markdown"`, `f"{provider}:native-pdf"` | 自由描述串 + 动态拼接 |
| `documents.py` 文件扩展名 | `".pdf"`, `".txt"`, `".docx"` | 文件系统常量，非语义枚举 |
| `settings.py` vector_backend | `"chroma"` | 单值，改为 `str = "chroma"` |

## 2. 兼容性保障

StrEnum（`class StrEnum(str, Enum)`）的关键特性：
- `LLMProvider.OPENAI == "openai"` → `True`（str 子类比较）
- `json.dumps(LLMProvider.OPENAI)` → `"\"openai\""`（JSON 序列化为字符串值）
- Pydantic v2 原生支持 StrEnum，OpenAPI schema 生成 `"enum": ["openai", "anthropic"]`
- pydantic-settings 从环境变量加载 StrEnum（字符串值自动匹配）
- StrEnum 实例的 `hash()` 与 str 相同，可用作 dict key

**结论**：API JSON 输出、前端交互、环境变量加载均保持完全兼容。

## 3. 枚举定义清单

### 3.1 全局共享枚举 → `models/enums.py`（新建）

```python
from enum import StrEnum

# ── LLM Provider（rerank 复用此枚举，不单独定义 RerankProvider）──
class LLMProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"

class EmbeddingProvider(StrEnum):
    LOCAL = "local"
    OLLAMA = "ollama"

# ── 检索 ──
class RetrievalMethod(StrEnum):
    VECTOR = "vector"
    BM25 = "bm25"
    HYBRID = "hybrid"

# ── Agent 引擎 ──
class ChatbotBackend(StrEnum):
    LANGCHAIN = "langchain"
    AGENT_SDK = "agent_sdk"

# ── Workflow 步骤状态（AgentStep.status）──
class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

# ── Workflow 持久化运行状态（sqlite workflows.status 列）──
class WorkflowRunState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    AWAITING_INPUT = "awaiting_input"
    INTERRUPTED = "interrupted"
    FAILED = "failed"

class SubsystemType(StrEnum):
    NEW = "new"
    EXTEND = "extend"
    EXISTING = "existing"

# ── Spec Loop ──
class SpecVerdict(StrEnum):
    PASS = "PASS"
    NEEDS_IMPROVEMENT = "NEEDS_IMPROVEMENT"
    FAIL = "FAIL"

# ── Chat ──
class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"

class ChatIntent(StrEnum):
    DIRECT = "direct"
    RAG = "rag"

# ── Health ──
class DependencyName(StrEnum):
    EMBEDDING = "embedding"
    AGENT_LLM = "agent_llm"
    CRITIC_LLM = "critic_llm"

class DependencyStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"

class FeatureName(StrEnum):
    CHAT = "chat"
    RAG_SEARCH = "rag_search"
    WORKFLOW = "workflow"

# ── Index Job ──
class IndexJobState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

# ── Document ──
class DocumentParseMode(StrEnum):
    LOCAL = "local"
    NATIVE = "native"
    HYBRID = "hybrid"

class LocalParser(StrEnum):
    PYPDF = "pypdf"
    DOCLING = "docling"

# ── Session Facts ──
class FactCategory(StrEnum):
    USER_DECISION = "user_decision"
    USER_PREFERENCE = "user_preference"
    PROJECT_CONTEXT = "project_context"
    OPEN_QUESTION = "open_question"

class FactStatus(StrEnum):
    ACTIVE = "active"
    RETRACTED = "retracted"

class FactAction(StrEnum):
    ADD = "add"
    AMEND = "amend"
    RETRACT = "retract"

# ── 截断原因 ──
class TruncationReason(StrEnum):
    TOOL_LOOP_TRUNCATED = "tool_loop_truncated"
    MAX_TOKENS_TRUNCATED = "max_tokens_truncated"
```

**共 20 个全局枚举。**（审计修正：删 RerankProvider 合并到 LLMProvider；新增 WorkflowRunState、FactAction）

### 3.2 模块特定枚举 → 就近定义

**`documents.py`：**
```python
class ContentFormat(StrEnum):
    MARKDOWN = "markdown"
    TEXT = "text"

class DocumentFormat(StrEnum):
    MARKDOWN = "markdown"
    TEXT = "text"
    PDF = "pdf"
    DOCX = "docx"

class ImageKind(StrEnum):
    EMBEDDED = "embedded"
    RELATIVE = "relative"
    REMOTE = "remote"
    DATA = "data"
```

**`agent/state.py` + `agent/heuristics.py`：**
```python
class BusinessDomain(StrEnum):
    USER = "user"
    ORDER = "order"
    PAYMENT = "payment"
    NOTIFICATION = "notification"
    CATALOG = "catalog"
    RISK = "risk"
    REPORTING = "reporting"
```

**`agent/factory.py`：**
```python
class BackendScope(StrEnum):
    CHATBOT = "chatbot"
    WORKFLOW = "workflow"
```

### 3.3 外部 API 字符串 → 常量集 / 镜像枚举（D 类）

> **审计修正（C1）**：`_finish_reason()` 的返回值可能透传外部 SDK 的任意值（如 `content_filter`、`end_turn`、`stop_sequence`），不可枚举化返回类型。`FinishReason` 仅作为**比较常量集**使用。

**`llm/types.py`（扩展）：**
```python
class FinishReason(StrEnum):
    """归一化后的 finish_reason 已知值常量集。
    
    注意：_finish_reason() 返回类型保持 str | None（可能透传未知外部值）。
    此枚举仅用于比较点（if x == FinishReason.LENGTH），不用于返回值类型标注。
    """
    LENGTH = "length"           # OpenAI 语义（含 Anthropic max_tokens 归一化）
    STOP = "stop"
    TOOL_CALLS = "tool_calls"

class ContentType(StrEnum):
    """Anthropic/OpenAI content block 的 type 字段。"""
    TEXT = "text"
```

**`agent_sdk/backend.py` 内部定义：**
```python
class ClaudeMsgSubtype(StrEnum):
    """claude-agent-sdk 消息流的 subtype 值（外部 SDK 定义）。"""
    INIT = "init"
    SUCCESS = "success"

class AnthropicEventType(StrEnum):
    """Anthropic streaming API 的 event type 值（外部 SDK 定义）。"""
    CONTENT_BLOCK_DELTA = "content_block_delta"

class AnthropicDeltaType(StrEnum):
    """Anthropic streaming delta 的 type 值（外部 SDK 定义）。"""
    TEXT_DELTA = "text_delta"
```

## 4. 受影响文件清单与改动详情

### 4.1 新建文件

| 文件 | 内容 |
|------|------|
| `models/enums.py` | 20 个全局共享 StrEnum |

### 4.2 改动文件（按优先级排序）

#### 第一层：Models（定义源头）

**`models/common.py`**
- 删除 `IndexJobState`, `DependencyName`, `DependencyStatus`, `FeatureName` 的 Literal 别名定义
- 从 `.enums` 导入对应 StrEnum
- `IndexJobStatus.status` → `IndexJobState`
- `DependencyHealth.name` → `DependencyName`，`.status` → `DependencyStatus`
- `FeatureAvailability.name` → `FeatureName`
- `IndexStats.backend` / `IndexStats.embedding_provider` → 保持 str（动态/统计值）

**`models/spec.py`**
- 删除 `Verdict = Literal[...]`（审计确认：`Verdict` 仅在此文件和 `__init__.py` 内部使用，无外部 `from ..models import Verdict` 引用，可直接改名）
- `Critique.verdict` → `SpecVerdict`
- `SpecAttempt.verdict` → `SpecVerdict`，默认值 `SpecVerdict.NEEDS_IMPROVEMENT`

**`models/workflow.py`**
- `AgentStep.status` → `StepStatus`，默认值 `StepStatus.PENDING`
- `Subsystem.type` → `SubsystemType`，默认值 `SubsystemType.NEW`

**`models/chat.py`**
- `ChatMessage.role` → `ChatRole`
- `MemoryRecord.role` → `ChatRole`
- `SessionFact.category` → `FactCategory`
- `SessionFact.status` → `FactStatus`，默认值 `FactStatus.ACTIVE`

**`models/payload.py`**
- `RagSettingsPayload`: `embedding_provider` → `EmbeddingProvider | None`，`retrieval_method` → `RetrievalMethod | None`，`rerank_provider` → `LLMProvider | None`（审计修正：复用 LLMProvider）
- `AgentSettingsPayload`: `chatbot_backend` → `ChatbotBackend | None`，`workflow_backend` → `ChatbotBackend | None`，`agent_provider` → `LLMProvider | None`，`critic_provider` → `LLMProvider | None`
- `DocumentSettingsPayload`: `parse_mode` → `DocumentParseMode | None`，`local_parser` → `LocalParser | None`

**`models/__init__.py`**
- 从 `.enums` 重新导出所有 20 个 StrEnum
- 删除 `Verdict` 的导出（已改名为 `SpecVerdict`）

#### 第二层：Settings

**`settings.py`**
- 删除 `from typing import Literal`
- `embedding_provider: EmbeddingProvider = EmbeddingProvider.LOCAL`
- `vector_backend: str = "chroma"`（单值，不枚举化）
- `chatbot_backend: ChatbotBackend = ChatbotBackend.LANGCHAIN`
- `workflow_backend: ChatbotBackend = ChatbotBackend.LANGCHAIN`
- `agent_provider: LLMProvider = Field(default=LLMProvider.OPENAI, validation_alias="LANGCHAIN_AGENT_PROVIDER")`
- `critic_provider: LLMProvider | None = None`
- `facts_provider: LLMProvider | None = None`
- `document_parse_mode: DocumentParseMode = DocumentParseMode.HYBRID`
- `document_local_parser: LocalParser = LocalParser.PYPDF`
- `retrieval_method: RetrievalMethod = RetrievalMethod.HYBRID`
- `rerank_provider: LLMProvider | None = None`（审计修正：复用 LLMProvider）

#### 第三层：Workflow 持久化状态（审计新增 W2）

**`workflow_store.py`**
- `:81` `WorkflowCreated(status="created")` → `WorkflowRunState.CREATED`
- 所有 `update_status(wid, "...")` 调用点 → `WorkflowRunState.*`

**`workflow_executor.py`**（14 处状态调用）
- `update_status(wid, "running")` → `WorkflowRunState.RUNNING`
- `update_status(wid, "failed")` → `WorkflowRunState.FAILED`
- `update_status(wid, "completed")` → `WorkflowRunState.COMPLETED`
- `update_status(wid, "awaiting_input")` → `WorkflowRunState.AWAITING_INPUT`
- `update_status(wid, "interrupted")` → `WorkflowRunState.INTERRUPTED`

**`api/routes/workflow.py`**（审计新增）
- `:56` `WorkflowCreated.status: str` → `WorkflowRunState`
- `:63` `WorkflowListItem.status: str` → `WorkflowRunState`
- `:79` `WorkflowDetail.status: str` → `WorkflowRunState`
- `:97` `DocumentCapabilitiesView.parse_mode: str` → `DocumentParseMode`
- `:146,217` `status="running"` → `WorkflowRunState.RUNNING`
- `:285` `row["status"] == "completed"` → `WorkflowRunState.COMPLETED`
- `:289,312` status 比较 → `WorkflowRunState.*`

#### 第四层：LLM

**`llm/types.py`**
- 新增 `FinishReason`（常量集）、`ContentType` StrEnum
- `ToolLoopResult.reason` → `TruncationReason | None`

**`llm/client.py`**
- `_finish_reason()` 返回类型保持 `str | None`（审计修正 C1）
  - `if fr == "max_tokens":` → `if fr == "max_tokens":`（保持，此为外部原始值比较）
  - `return "length"` → `return FinishReason.LENGTH.value`（即 `"length"`，语义不变）
  - `return fr`（透传未知值，保持 str）
- `if finish_reason == "length":` → `if finish_reason == FinishReason.LENGTH:`
- `if _finish_reason(resp) != "length":` → `if _finish_reason(resp) != FinishReason.LENGTH:`
- `self.settings.agent_provider == "anthropic"` → `LLMProvider.ANTHROPIC`
- `self.settings.agent_provider == "openai"` → `LLMProvider.OPENAI`
- `role == "system"/"user"/"assistant"` → `ChatRole.*`
- `block.type == "text"` / `getattr(block, "type", None) == "text"` → `ContentType.TEXT`

**`llm/parsing.py`** — 检查是否有 string literal（预期无）

#### 第五层：业务逻辑

**`intent.py`**
- 删除 `ChatIntent = Literal[...]`
- `IntentDecision.intent` → 从 `models.enums` 导入的 `ChatIntent`
- `IntentDecision("direct", ...)` → `IntentDecision(ChatIntent.DIRECT, ...)`
- `IntentDecision("rag", ...)` → `IntentDecision(ChatIntent.RAG, ...)`
- `if intent not in ("direct", "rag"):` → `if intent not in [e.value for e in ChatIntent]:`
- JSON schema `"enum": ["direct", "rag"]` → `[e.value for e in ChatIntent]`

**`health.py`**
- `name: DependencyName = "embedding"` → `DependencyName.EMBEDDING`
- `settings.embedding_provider == "local"` → `EmbeddingProvider.LOCAL`
- `name == "critic_llm"` → `DependencyName.CRITIC_LLM`
- `name == "agent_llm"` → `DependencyName.AGENT_LLM`
- `provider == "openai"` / `provider == "anthropic"` → `LLMProvider.*`
- `status="ok"`, `status="down"`, `status="unknown"` → `DependencyStatus.*`
- `by_name.get("agent_llm")` → `by_name.get(DependencyName.AGENT_LLM)`
- `by_name.get("embedding")` → `by_name.get(DependencyName.EMBEDDING)`
- `by_name.get("agent_llm").status != "down"` → `DependencyStatus.DOWN`
- `by_name.get("embedding").status == "ok"` → `DependencyStatus.OK`
- `name="chat"`, `name="rag_search"`, `name="workflow"` → `FeatureName.*`

**`documents.py`**
- 新增 `ContentFormat`, `DocumentFormat`, `ImageKind` StrEnum（模块内）
- 删除 Literal 别名 `ContentFormat`, `DocumentFormat`, `DocumentParseMode`
- `mode == "local"` → `DocumentParseMode.LOCAL`；`mode == "native"` → `DocumentParseMode.NATIVE`
- `content_format == "markdown"` → `ContentFormat.MARKDOWN`
- `local_parser == "docling"` → `LocalParser.DOCLING`；`parser == "pypdf"` → `LocalParser.PYPDF`
- `DocumentArtifact.parser` → **保留 str**（动态拼接值如 `f"{provider}:native-pdf"`）
- `suffix == ".pdf"` 等 → **保留 str**（文件扩展名）

**`vector_store.py`**
- `method == "vector"` → `RetrievalMethod.VECTOR`
- `method == "bm25"` → `RetrievalMethod.BM25`
- `self.settings.embedding_provider == "local"` → `EmbeddingProvider.LOCAL`

**`retrieval.py`**（审计修正 W4）
- `:141` `provider == "openai"` → `LLMProvider.OPENAI`
- `:157` `provider == "anthropic"` → `LLMProvider.ANTHROPIC`
- 注意：`provider = settings.rerank_provider or settings.agent_provider` 两者均已改为 `LLMProvider` 类型

**`embeddings.py`**（审计修正 W5）
- `:53` `self.settings.embedding_provider == "local"` → `EmbeddingProvider.LOCAL`
- `:55` `self.settings.embedding_provider == "ollama"` → `EmbeddingProvider.OLLAMA`

**`agent/state.py`**
- 新增 `BusinessDomain` StrEnum（模块内）
- `DOMAIN_HINTS` → `dict[BusinessDomain, list[str]]`

**`agent/heuristics.py`**
- `matched_domains` 返回 `dict[BusinessDomain, list[str]]`
- `responsibility_for(domain: BusinessDomain)` — 内部 dict key 改为枚举
- `capabilities_for`, `entities_for` 同上
- `raw_type if raw_type in ("new", "extend", "existing")` → `raw_type if raw_type in [e.value for e in SubsystemType]`
- `subsystem_schema()` 中的 `"enum": ["new", "extend", "existing"]` → `[e.value for e in SubsystemType]`

**`agent/langchain_chat.py`**
- `decision.intent == "direct"` → `ChatIntent.DIRECT`
- `decision.reason` → **保留 str**（自由文本 + 动态拼接）

**`agent/factory.py`**
- 新增 `BackendScope` StrEnum（模块内）
- `backend_name == "agent_sdk"` → `ChatbotBackend.AGENT_SDK`
- `scope == "chatbot"` → `BackendScope.CHATBOT`

**`agent/agent.py`**（审计修正 W3）
- `:40` `scope="workflow"` → `scope=BackendScope.WORKFLOW`
- `:75` `AgentStep(name=..., status="completed", ...)` → `StepStatus.COMPLETED`

**`agent/nodes/understand.py`**
- `result.reason == "max_tokens_truncated"` → `TruncationReason.MAX_TOKENS_TRUNCATED`

**`agent/nodes/`** 其他节点（evaluate, generate, identify, retrieve）— 检查并替换

**`specs/steps.py`**（审计修正 W7）
- `result.reason == "max_tokens_truncated"` → `TruncationReason.MAX_TOKENS_TRUNCATED`
- `:97-99` verdict 校验：`if verdict not in ("PASS", "NEEDS_IMPROVEMENT", "FAIL"):` → `if verdict not in [e.value for e in SpecVerdict]:`，默认值 `verdict = parsed.get("verdict", SpecVerdict.NEEDS_IMPROVEMENT.value)`

**`specs/loop.py`**（审计修正 W8）
- `:50` `SpecAttempt(version=i, verdict="NEEDS_IMPROVEMENT", ...)` → `SpecAttempt(version=i, verdict=SpecVerdict.NEEDS_IMPROVEMENT, ...)`
- `:65` `critique.verdict == "PASS"` → `SpecVerdict.PASS`

**`specs/rubric.py`**（审计修正 W6）
- `:27` `_critique_schema()` 中 `"verdict": {"type": "string", "enum": ["PASS", "NEEDS_IMPROVEMENT", "FAIL"]}` → `"enum": [e.value for e in SpecVerdict]`

**`memory/facts.py`**
- `_CATEGORY_PRIORITY` dict key → `FactCategory.*`（4 个 key）
- `_CATEGORY_HEADERS` dict key → `FactCategory.*`（4 个 key）
- `:178` schema hint `"action": "add | amend | retract"` → 保持（LLM schema 文本）
- `:227` `action = item.get("action", "add")` → `item.get("action", FactAction.ADD.value)`
- `:230` `if action == "retract":` → `FactAction.RETRACT`
- SQL 查询中的 `status='active'` / `status='retracted'` → 使用 `FactStatus.ACTIVE.value` / `FactStatus.RETRACTED.value`（共 6 处）
- SQL 查询参数化值（如 `category=?`）的调用方传入的值 → 确保传入 `.value` 或 StrEnum 实例（StrEnum 作为 str 子类，直接传入也可）

**`agent_sdk/backend.py`**
- 新增 `ClaudeMsgSubtype`, `AnthropicEventType`, `AnthropicDeltaType` StrEnum（模块内）
- `msg.subtype != "success"` → `ClaudeMsgSubtype.SUCCESS`
- `msg.subtype == "init"` → `ClaudeMsgSubtype.INIT`（2 处：`:419`, `:548`）
- `event.get("type") == "content_block_delta"` → `AnthropicEventType.CONTENT_BLOCK_DELTA`
- `event.get("delta", {}).get("type") == "text_delta"` → `AnthropicDeltaType.TEXT_DELTA`
- `NodeExecutionResult.reason` → **保留 str**（自由文本 + 动态拼接）

**`index_supervisor.py`**
- `status.status == "running"` → `IndexJobState.RUNNING`
- `rag_available()` 返回的 reason 字符串（`"reindexing"`, `"index_unavailable"`）→ **保留 str**（跨模块契约）

**`api/routes/chat.py`**（审计修正 W3）
- `:39,60` `scope="chatbot"` → `scope=BackendScope.CHATBOT`

**`api/routes/`** 其他文件 — 检查并替换（大部分已通过 model 层传导）

### 4.3 特别注意点

1. **`_finish_reason` 不枚举化返回类型**（审计 C1）：返回类型保持 `str | None`。`FinishReason` 仅作为比较常量集（`== FinishReason.LENGTH`），因为 `_finish_reason` 会透传未知外部 SDK 值（如 `content_filter`、`end_turn`）。
2. **SQL 查询中的字符串**：`memory/facts.py` 中 `status='active'` 等 SQL 内嵌值需用 `FactStatus.ACTIVE.value` 保持原始字符串值。
3. **JSON schema 中的 enum**：以下 4 处给 LLM 的 schema 含 `"enum": [...]`，应从 StrEnum 动态生成 `[e.value for e in EnumType]`：
   - `heuristics.py:99`（subsystem type → `SubsystemType`）
   - `intent.py:72`（intent → `ChatIntent`）
   - `rubric.py:27`（verdict → `SpecVerdict`）
   - `tools/registry.py`（如有）
4. **LLM 输出校验**：`specs/steps.py:97-99` 的 verdict 校验和 `intent.py:80` 的 intent 校验，需用 `[e.value for e in EnumType]` 或 `try: EnumType(val) except ValueError` 适配。
5. **LangGraph state dict key**：`current_step`、`needs_rework` 等 key 不改（E 类排除）。
6. **`validation_alias` 保持**：`settings.py` 中 `agent_provider` 的 `validation_alias="LANGCHAIN_AGENT_PROVIDER"` 不变。
7. **`is` 比较**：StrEnum 实例的 `is` 比较不成立（`LLMProvider.OPENAI is "openai"` → False），但项目中无 `is` 比较（已确认），仅用 `==`。

## 5. 向后兼容性处理

### 5.1 models/__init__.py 的重新导出

```python
from .enums import (
    ChatbotBackend, ChatIntent, ChatRole, DependencyName, DependencyStatus,
    DocumentParseMode, EmbeddingProvider, FactAction, FactCategory, FactStatus,
    FeatureName, IndexJobState, LLMProvider, LocalParser, RetrievalMethod,
    SpecVerdict, StepStatus, SubsystemType, TruncationReason, WorkflowRunState,
)
```

### 5.2 Verdict 改名说明

`Verdict = Literal[...]` 仅在 `models/spec.py` 和 `models/__init__.py` 内部使用（grep 确认无外部 `from ..models import Verdict` 引用）。直接改名为 `SpecVerdict`，无需向后兼容别名。

### 5.3 RerankProvider 合并说明

`RerankProvider` 与 `LLMProvider` 成员值完全相同（`openai`/`anthropic`），且在 `retrieval.py:135` 中 `provider = settings.rerank_provider or settings.agent_provider` 表明两者运行时可互换。故删除 `RerankProvider`，`rerank_provider` 字段复用 `LLMProvider`。

## 6. 测试策略

### 6.1 回归测试

现有 53 个测试文件全部必须通过。StrEnum 的 `==` 兼容性确保大部分测试不需要修改。

需要重点检查的测试：
- `test_settings_*.py` — Settings 字段类型和默认值
- `test_intent.py` — IntentDecision.intent 比较
- `test_spec_loop.py` / `test_evaluation.py` — verdict 比较
- `test_workflow_api.py` — workflow 状态机（审计 W2 涉及）
- `test_workflow_executor.py` — workflow 状态转换
- `test_workflow_store.py` — workflow 持久化
- `test_facts_*.py` — fact action 和 status

### 6.2 验证步骤

1. `cd backend && python -m pytest -q` — 全量测试通过
2. `cd backend && python -m ruff check src/` — 无 lint 错误
3. 手动验证 API JSON 输出不变（StrEnum 序列化为字符串值）
4. 手动验证 pydantic-settings 从环境变量加载正常

## 7. 实施顺序

按依赖方向自底向上：

1. 新建 `models/enums.py` — 定义全部 20 个枚举
2. 更新 `models/` 各文件 — 替换 Literal 字段类型（common, spec, workflow, chat, payload）
3. 更新 `models/__init__.py` — 重新导出
4. 更新 `settings.py` — 替换 Literal 字段
5. 更新 `llm/types.py` — 新增外部 API 常量集/枚举
6. 更新 `llm/client.py` — 替换字符串比较
7. 更新 workflow 状态机（workflow_store, workflow_executor, api/routes/workflow）
8. 更新业务逻辑文件（intent, health, documents, vector_store, embeddings, retrieval, specs/, agent/, memory/, agent_sdk/, api/routes/）
9. 运行全量测试 + lint
