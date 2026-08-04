# 后端枚举化重构设计

> 日期：2026-08-05  
> 状态：Draft → 待审计

## 1. 目标

消除后端 Python 代码中的魔法字符串（string literal），使用 Python 3.12 `StrEnum` 替代 `Literal[...]` 类型别名和内联字符串比较，提升类型安全性和代码可维护性。

### 范围

| 类别 | 描述 | 纳入 |
|------|------|------|
| A. Literal 类型别名 | `Verdict = Literal["PASS","FAIL"]` 等 9 个 | ✅ |
| B. 内联 Literal | `Literal["openai","anthropic"]` 等 20+ 处 | ✅ |
| C. String 比较 | `if x == "anthropic":` 等 50+ 处 | ✅ |
| D. 外部 API 字符串 | `finish_reason == "length"`, `msg.subtype == "init"` 等 | ✅ |
| E. Dict 魔法 key | `state["answer_text"]` 等 LangGraph 状态 key | ❌ 不纳入 |

### 不做的事

- 不改 Pydantic ↔ dataclass 的架构边界（Pydantic 用于 API/Settings，dataclass 用于内部结构——符合社区共识）
- 不改 LangGraph `TypedDict` 状态 key（E 类）
- 不改前端代码（StrEnum 的 JSON 序列化值与现有字符串完全一致）
- 不新增/删除功能，纯重构

## 2. 兼容性保障

StrEnum（`class StrEnum(str, Enum)`）的关键特性：
- `LLMProvider.OPENAI == "openai"` → `True`（str 子类比较）
- `json.dumps(LLMProvider.OPENAI)` → `"\"openai\""`（JSON 序列化为字符串值）
- Pydantic v2 原生支持 StrEnum，OpenAPI schema 生成 `"enum": ["openai", "anthropic"]`
- pydantic-settings 从环境变量加载 StrEnum（字符串值自动匹配）

**结论**：API JSON 输出、前端交互、环境变量加载均保持完全兼容。

## 3. 枚举定义清单

### 3.1 全局共享枚举 → `models/enums.py`（新建）

这些枚举被 2 个以上模块引用，放在集中位置：

```python
from enum import StrEnum

# ── LLM / Embedding Provider ──
class LLMProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"

class EmbeddingProvider(StrEnum):
    LOCAL = "local"
    OLLAMA = "ollama"

# ── 检索 / 重排 ──
class RetrievalMethod(StrEnum):
    VECTOR = "vector"
    BM25 = "bm25"
    HYBRID = "hybrid"

class RerankProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"

# ── Agent 引擎 ──
class ChatbotBackend(StrEnum):
    LANGCHAIN = "langchain"
    AGENT_SDK = "agent_sdk"

# ── Workflow 状态 ──
class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
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

# ── 截断原因 ──
class TruncationReason(StrEnum):
    TOOL_LOOP_TRUNCATED = "tool_loop_truncated"
    MAX_TOKENS_TRUNCATED = "max_tokens_truncated"
```

**共 19 个枚举。**

### 3.2 模块特定枚举 → 就近定义

仅在一个模块内使用的枚举保留在原模块：

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

### 3.3 外部 API 字符串 → 模块内镜像枚举（D 类）

这些字符串的值由外部 SDK（Anthropic/OpenAI/Claude Agent SDK）定义，我们提取为枚举常量以便类型安全引用，但标注为「外部值的镜像」：

**`llm/types.py`（扩展）：**
```python
class FinishReason(StrEnum):
    """langchain response_metadata 中归一化后的 finish_reason 值。"""
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
| `models/enums.py` | 19 个全局共享 StrEnum |

### 4.2 改动文件（按优先级排序）

#### 第一层：Models（定义源头）

**`models/common.py`**
- 删除 `IndexJobState`, `DependencyName`, `DependencyStatus`, `FeatureName` 的 Literal 别名
- 从 `..enums`（或 `.enums`，取决于相对位置）导入对应 StrEnum
- `IndexJobStatus.status` → `IndexJobState`（StrEnum）
- `DependencyHealth.name` → `DependencyName`，`.status` → `DependencyStatus`
- `FeatureAvailability.name` → `FeatureName`
- `IndexStats.backend` → 保持 str（动态值，不枚举化）

**`models/spec.py`**
- 删除 `Verdict = Literal[...]`
- `Critique.verdict` → `SpecVerdict`
- `SpecAttempt.verdict` → `SpecVerdict`，默认值改为 `SpecVerdict.NEEDS_IMPROVEMENT`

**`models/workflow.py`**
- `AgentStep.status` → `StepStatus`，默认值 `StepStatus.PENDING`
- `Subsystem.type` → `SubsystemType`，默认值 `SubsystemType.NEW`

**`models/chat.py`**
- `ChatMessage.role` → `ChatRole`
- `MemoryRecord.role` → `ChatRole`
- `SessionFact.category` → `FactCategory`
- `SessionFact.status` → `FactStatus`，默认值 `FactStatus.ACTIVE`

**`models/payload.py`**
- 所有 `Literal[...]` 字段 → 对应 StrEnum
- `RagSettingsPayload`: `embedding_provider`, `retrieval_method`, `rerank_provider`
- `AgentSettingsPayload`: `chatbot_backend`, `workflow_backend`, `agent_provider`, `critic_provider`
- `DocumentSettingsPayload`: `parse_mode`, `local_parser`
- 可选字段（`... | None`）保持 `StrEnum | None` 形式

**`models/__init__.py`**
- 重新导出 enums 中的所有 StrEnum（保持向后兼容）

#### 第二层：Settings

**`settings.py`**
- 删除 `from typing import Literal`
- `embedding_provider: StrEnum = EmbeddingProvider.LOCAL`
- `vector_backend` → 改为 `str = "chroma"`（单值 Literal，不值得枚举）
- `chatbot_backend` → `ChatbotBackend.LANGCHAIN`
- `workflow_backend` → `ChatbotBackend.LANGCHAIN`
- `agent_provider` → `LLMProvider.OPENAI`（保留 `validation_alias`）
- `critic_provider` → `LLMProvider | None`
- `facts_provider` → `LLMProvider | None`
- `document_parse_mode` → `DocumentParseMode.HYBRID`
- `document_local_parser` → `LocalParser.PYPDF`
- `retrieval_method` → `RetrievalMethod.HYBRID`
- `rerank_provider` → `RerankProvider | None`

#### 第三层：业务逻辑

**`llm/types.py`**
- 新增 `FinishReason`, `ContentType` StrEnum
- `ToolLoopResult.reason` → `TruncationReason | None`

**`llm/client.py`**
- `_finish_reason()`：`"max_tokens"` → `FinishReason.LENGTH`（注意归一化逻辑）或保留原始值用常量
- `if fr == "max_tokens":` → 比较 FinishReason 或常量
- `return "length"` → `return FinishReason.LENGTH`
- `if finish_reason == "length":` → `FinishReason.LENGTH`
- `self.settings.agent_provider == "anthropic"` → `LLMProvider.ANTHROPIC`
- `self.settings.agent_provider == "openai"` → `LLMProvider.OPENAI`
- `role == "system"/"user"/"assistant"` → `ChatRole.*`
- `block.type == "text"` / `getattr(block, "type", None) == "text"` → `ContentType.TEXT`

**`llm/parsing.py`**（检查是否有 string literal）

**`intent.py`**
- 删除 `ChatIntent = Literal[...]`
- `IntentDecision.intent` → 从 models.enums 导入的 `ChatIntent` StrEnum
- `IntentDecision("direct", ...)` → `IntentDecision(ChatIntent.DIRECT, ...)`
- `parsed.get("intent")` 后的 `if intent not in ("direct", "rag"):` → 用 StrEnum 值验证

**`health.py`**
- `name: DependencyName = "embedding"` → `DependencyName.EMBEDDING`
- `settings.embedding_provider == "local"` → `EmbeddingProvider.LOCAL`
- `name == "critic_llm"` → `DependencyName.CRITIC_LLM`
- `name == "agent_llm"` → `DependencyName.AGENT_LLM`
- `provider == "openai"` / `provider == "anthropic"` → `LLMProvider.*`
- `status="ok"`, `status="down"`, `status="unknown"` → `DependencyStatus.*`
- `by_name.get("agent_llm")` → `DependencyName.AGENT_LLM`
- `by_name.get("embedding")` → `DependencyName.EMBEDDING`
- `name="chat"`, `name="rag_search"`, `name="workflow"` → `FeatureName.*`

**`documents.py`**
- 新增 `ContentFormat`, `DocumentFormat`, `ImageKind` StrEnum（模块内）
- 删除 Literal 别名 `ContentFormat`, `DocumentFormat`, `DocumentParseMode`
- 函数参数中的 `mode`, `local_parser`, `content_format` → 对应 StrEnum
- `suffix == ".pdf"`, `suffix == ".txt"` 等 → 保持字符串（文件扩展名不枚举化）
- `mode == "local"`, `mode == "native"` → `DocumentParseMode.*`
- `content_format == "markdown"` → `ContentFormat.MARKDOWN`
- `local_parser == "docling"` → `LocalParser.DOCLING`
- `parser == "pypdf"` → `LocalParser.PYPDF`

**`vector_store.py`**
- `method == "vector"` → `RetrievalMethod.VECTOR`
- `method == "bm25"` → `RetrievalMethod.BM25`
- `self.settings.embedding_provider == "local"` → `EmbeddingProvider.LOCAL`

**`retrieval.py`**（检查 string literal）

**`embeddings.py`**
- `self.settings.embedding_provider == "local"` → `EmbeddingProvider.LOCAL`

**`agent/state.py`**
- 新增 `BusinessDomain` StrEnum
- `DOMAIN_HINTS` → `dict[BusinessDomain, list[str]]`

**`agent/heuristics.py`**
- `matched_domains` 返回 `dict[BusinessDomain, list[str]]`
- `responsibility_for(domain: BusinessDomain)` — 内部 dict key 改为枚举
- `capabilities_for`, `entities_for` 同上
- `raw_type if raw_type in ("new", "extend", "existing")` → `SubsystemType` 值验证
- `subsystem_schema()` 中的 JSON schema `"enum": ["new", "extend", "existing"]` → 从 `SubsystemType` 动态生成

**`agent/langchain_chat.py`**
- `decision.intent == "direct"` → `ChatIntent.DIRECT`
- `decision.reason` 字段 → **不枚举化**（reason 是自由文本描述，含动态拼接如 `"llm:{...}"`，保持 str）

**`agent/factory.py`**
- `backend_name == "agent_sdk"` → `ChatbotBackend.AGENT_SDK`
- `scope == "chatbot"` → 新增模块内 `BackendScope(StrEnum)` 枚举（`CHATBOT = "chatbot"`, `WORKFLOW = "workflow"`）

**`agent/nodes/understand.py`**
- `result.reason == "max_tokens_truncated"` → `TruncationReason.MAX_TOKENS_TRUNCATED`

**`agent/nodes/`** 其他节点（evaluate, generate, identify, retrieve）— 检查并替换

**`specs/steps.py`**
- `result.reason == "max_tokens_truncated"` → `TruncationReason.MAX_TOKENS_TRUNCATED`

**`specs/loop.py`**
- `critique.verdict == "PASS"` → `SpecVerdict.PASS`

**`memory/facts.py`**
- `_CATEGORY_PRIORITY` dict key → `FactCategory.*`
- `_CATEGORY_HEADERS` dict key → `FactCategory.*`
- `action == "retract"` → 新增 `FactAction(StrEnum)` 枚举（`RETRACT = "retract"`, `UPSERT = "upsert"` 等，根据实际调用值确定）
- SQL 查询中的 `status='active'` → 使用 `FactStatus.ACTIVE.value`（SQL 需要原始字符串值）

**`agent_sdk/backend.py`**
- 新增 `ClaudeMsgSubtype`, `AnthropicEventType`, `AnthropicDeltaType` StrEnum（模块内）
- `msg.subtype != "success"` → `ClaudeMsgSubtype.SUCCESS`
- `msg.subtype == "init"` → `ClaudeMsgSubtype.INIT`
- `event.get("type") == "content_block_delta"` → `AnthropicEventType.CONTENT_BLOCK_DELTA`
- `event.get("delta", {}).get("type") == "text_delta"` → `AnthropicDeltaType.TEXT_DELTA`

**`agent_sdk/tools.py`**（检查 string literal）

**`tools/handlers.py`**（检查 string literal）

**`tools/registry.py`**
- JSON schema 中的 `"enum": [...]` → 动态生成或保持（给 LLM 的 schema）

**`index_supervisor.py`**
- `status.status == "running"` → `IndexJobState.RUNNING`

#### 第四层：API Routes

**`api/routes/` 各文件** — 检查并替换 string literal（大部分已通过 model 层传导）

### 4.3 特别注意点

1. **SQL 查询中的字符串**：`memory/facts.py` 中 `status='active'` 等 SQL 内嵌值需用 `FactStatus.ACTIVE.value` 保持原始字符串
2. **JSON schema 中的 enum**：`heuristics.py` 和 `registry.py` 中给 LLM 的 schema 含 `"enum": ["direct", "rag"]`，应从 StrEnum 动态生成 `[e.value for e in ChatIntent]`
3. **`_finish_reason` 归一化逻辑**：`llm/client.py:34` 将 Anthropic `"max_tokens"` 映射为 `"length"`，需保持语义不变
4. **LangGraph state dict key**：`current_step`、`needs_rework` 等 key 不改（E 类排除）
5. **`validation_alias` 保持**：`settings.py` 中 `agent_provider` 的 `validation_alias="LANGCHAIN_AGENT_PROVIDER"` 不变

## 5. 向后兼容性处理

### 5.1 models/__init__.py 的重新导出

```python
from .enums import (
    ChatbotBackend, ChatIntent, ChatRole, DependencyName, DependencyStatus,
    DocumentParseMode, EmbeddingProvider, FactCategory, FactStatus,
    FeatureName, IndexJobState, LLMProvider, LocalParser, RerankProvider,
    RetrievalMethod, SpecVerdict, StepStatus, SubsystemType, TruncationReason,
)
```

确保现有 `from ..models import Verdict` 类导入路径仍可用（通过重新导出）。

### 5.2 已删除的 Literal 别名兼容

原有 `Verdict = Literal[...]` 别名被 `SpecVerdict` StrEnum 替代。所有引用 `Verdict` 的地方改为 `SpecVerdict`。由于 `SpecVerdict.PASS == "PASS"` 为 True，运行时比较行为不变。

## 6. 测试策略

### 6.1 回归测试

现有 40+ 测试文件全部必须通过。StrEnum 的 `==` 兼容性确保大部分测试不需要修改。

需要检查并可能更新的测试：
- `test_settings_*.py` — 如果测试硬编码了字符串值用于 Settings 比较
- `test_intent.py` — IntentDecision 的 intent 字段比较
- `test_evaluation.py` — verdict 比较
- 其他涉及枚举字段比较的测试

### 6.2 验证步骤

1. `cd backend && python -m pytest -q` — 全量测试通过
2. `cd backend && python -m ruff check src/` — 无 lint 错误
3. 手动验证 API JSON 输出不变（StrEnum 序列化为字符串值）
4. 手动验证 pydantic-settings 从环境变量加载正常

## 7. 实施顺序

按依赖方向自底向上：

1. 新建 `models/enums.py` — 定义全部 19 个枚举
2. 更新 `models/` 各文件 — 替换 Literal 字段类型
3. 更新 `models/__init__.py` — 重新导出
4. 更新 `settings.py` — 替换 Literal 字段
5. 更新 `llm/types.py` — 新增外部 API 枚举
6. 更新 `llm/client.py` — 替换字符串比较
7. 更新业务逻辑文件（intent, health, documents, vector_store, embeddings, specs/, agent/, memory/, agent_sdk/）
8. 更新模块特定枚举（documents.py, agent/state.py）
9. 运行全量测试 + lint
