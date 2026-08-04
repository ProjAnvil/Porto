# 后端枚举化重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将后端所有语义字符串迁移为 Python 3.12 `StrEnum`，消除魔法字符串，保持 100% 向后兼容。

**Architecture:** 自底向上分层迁移：先建 `models/enums.py` 集中定义枚举，再逐层替换 models → settings → llm → workflow → 业务逻辑。StrEnum 是 str 子类，`==` 比较和 JSON 序列化与字符串完全兼容，中间状态安全。

**Tech Stack:** Python 3.12, Pydantic v2, pydantic-settings, FastAPI, LangGraph, StrEnum

**Spec:** `docs/superpowers/specs/2026-08-05-backend-enum-refactor-design.md`

## Global Constraints

- Python `>=3.12`（StrEnum 自 3.11 可用）
- Pydantic `>=2.11.0`，pydantic-settings `>=2.10.0`
- StrEnum 成员值必须与现有字符串值**完全匹配**（大小写敏感）
- SQL 内嵌值用 `EnumMember.value` 保持原始字符串
- `_finish_reason()` 返回类型保持 `str | None`（不枚举化）
- LangGraph `TypedDict` state key 不改
- 前端不改（StrEnum JSON 序列化值 = 原字符串）
- 每个 Task 结束必须通过相关测试 + `ruff check`
- 所有源文件在 `backend/src/porto_chatbot/` 下
- 测试命令统一在 `backend/` 目录运行：`cd backend && python -m pytest tests/<pattern> -q`

---

### Task 1: 新建 models/enums.py

**Files:**
- Create: `backend/src/porto_chatbot/models/enums.py`

**Interfaces:**
- Produces: 20 个全局共享 StrEnum（供 Task 2+ 导入）

- [ ] **Step 1: 创建 enums.py**

创建 `backend/src/porto_chatbot/models/enums.py`，内容为 spec §3.1 中定义的全部 20 个 StrEnum。关键要点：
- `from enum import StrEnum`
- 每个 enum 的成员值与 spec 完全一致
- `LLMProvider` 包含 `OPENAI = "openai"` 和 `ANTHROPIC = "anthropic"`（rerank 复用此枚举）
- `WorkflowRunState` 包含 6 个成员：CREATED, RUNNING, COMPLETED, AWAITING_INPUT, INTERRUPTED, FAILED
- `FactAction` 成员为 `ADD = "add"`, `AMEND = "amend"`, `RETRACT = "retract"`（不是 UPSERT）

- [ ] **Step 2: 验证导入**

```bash
cd backend && python -c "from porto_chatbot.models.enums import *; print('OK')"
```
Expected: `OK`，无 ImportError

- [ ] **Step 3: Commit**

```bash
git add backend/src/porto_chatbot/models/enums.py
git commit -m "refactor: add models/enums.py with 20 StrEnum definitions"
```

---

### Task 2: 更新 models 层（common, spec, workflow, chat, payload, __init__）

**Files:**
- Modify: `backend/src/porto_chatbot/models/common.py`
- Modify: `backend/src/porto_chatbot/models/spec.py`
- Modify: `backend/src/porto_chatbot/models/workflow.py`
- Modify: `backend/src/porto_chatbot/models/chat.py`
- Modify: `backend/src/porto_chatbot/models/payload.py`
- Modify: `backend/src/porto_chatbot/models/__init__.py`

**Interfaces:**
- Consumes: Task 1 的 `models/enums.py`
- Produces: 所有 model 字段从 `Literal[...]` 改为 StrEnum 类型；`__init__.py` 重新导出全部 20 个枚举

- [ ] **Step 1: 更新 common.py**

- 删除 `IndexJobState`, `DependencyName`, `DependencyStatus`, `FeatureName` 的 Literal 定义（行 29, 49-51）
- 添加 `from .enums import IndexJobState, DependencyName, DependencyStatus, FeatureName`
- `IndexJobStatus.status: IndexJobState = "idle"` → `IndexJobStatus.status: IndexJobState = IndexJobState.IDLE`
- `DependencyHealth.status: DependencyStatus = "unknown"` → `DependencyStatus.UNKNOWN`
- `IndexStats.backend` 和 `IndexStats.embedding_provider` → 保持 str（动态/统计值，不改）

- [ ] **Step 2: 更新 spec.py**

- 删除 `Verdict = Literal["PASS", "NEEDS_IMPROVEMENT", "FAIL"]`（行 9）
- 添加 `from .enums import SpecVerdict`
- `Critique.verdict: Verdict` → `Critique.verdict: SpecVerdict`
- `SpecAttempt.verdict: Verdict = "NEEDS_IMPROVEMENT"` → `SpecAttempt.verdict: SpecVerdict = SpecVerdict.NEEDS_IMPROVEMENT`
- 删除 `from typing import Literal`（如果不再使用）

- [ ] **Step 3: 更新 workflow.py**

- 添加 `from .enums import StepStatus, SubsystemType`
- `AgentStep.status: Literal["pending", "running", "completed", "failed"] = "pending"` → `AgentStep.status: StepStatus = StepStatus.PENDING`
- `Subsystem.type: Literal["new", "extend", "existing"] = "new"` → `Subsystem.type: SubsystemType = SubsystemType.NEW`
- 删除 `from typing import Literal`（如果不再使用）

- [ ] **Step 4: 更新 chat.py**

- 添加 `from .enums import ChatRole, FactCategory, FactStatus`
- `ChatMessage.role: Literal["user", "assistant", "system"]` → `ChatMessage.role: ChatRole`
- `MemoryRecord.role: Literal["user", "assistant", "system"]` → `MemoryRecord.role: ChatRole`
- `SessionFact.category: Literal["user_decision", ...]` → `SessionFact.category: FactCategory`
- `SessionFact.status: Literal["active", "retracted"] = "active"` → `SessionFact.status: FactStatus = FactStatus.ACTIVE`
- 删除 `from typing import Literal`（如果不再使用）

- [ ] **Step 5: 更新 payload.py**

- 添加 `from .enums import (EmbeddingProvider, RetrievalMethod, LLMProvider, ChatbotBackend, DocumentParseMode, LocalParser)`
- `RagSettingsPayload`:
  - `embedding_provider: Literal["local", "ollama"] | None` → `EmbeddingProvider | None`
  - `retrieval_method: Literal["vector", "bm25", "hybrid"] | None` → `RetrievalMethod | None`
  - `rerank_provider: Literal["openai", "anthropic"] | None` → `LLMProvider | None`
- `AgentSettingsPayload`:
  - `chatbot_backend: Literal["langchain", "agent_sdk"] | None` → `ChatbotBackend | None`
  - `workflow_backend` → 同上
  - `agent_provider: Literal["openai", "anthropic"] | None` → `LLMProvider | None`
  - `critic_provider` → 同上
- `DocumentSettingsPayload`:
  - `parse_mode: Literal["local", "native", "hybrid"] | None` → `DocumentParseMode | None`
  - `local_parser: Literal["pypdf", "docling"] | None` → `LocalParser | None`
- 删除 `from typing import Literal`

- [ ] **Step 6: 更新 __init__.py**

- 从 `.enums` 导入全部 20 个 StrEnum 并重新导出
- 删除 `Verdict` 的导出（已改名为 `SpecVerdict`）
- 保留现有的 model 导出（ChatMessage, ChatRequest, Critique 等）

- [ ] **Step 7: 运行测试**

```bash
cd backend && python -m pytest tests/test_settings_fields.py tests/test_settings_truncation.py tests/test_settings_llm.py tests/test_settings_backend.py -q
```
Expected: 全部 PASS

- [ ] **Step 8: Lint 检查**

```bash
cd backend && python -m ruff check src/porto_chatbot/models/
```
Expected: 无错误

- [ ] **Step 9: Commit**

```bash
git add backend/src/porto_chatbot/models/
git commit -m "refactor(models): replace Literal types with StrEnum across all model files"
```

---

### Task 3: 更新 settings.py

**Files:**
- Modify: `backend/src/porto_chatbot/settings.py`

**Interfaces:**
- Consumes: Task 2 的 StrEnum（经 `from .models.enums import ...` 或 `from .models import ...`）
- Produces: Settings 字段从 `Literal[...]` 改为 StrEnum 类型

- [ ] **Step 1: 替换所有 Literal 字段**

- 删除 `from typing import Literal`
- 添加 `from .models.enums import (EmbeddingProvider, ChatbotBackend, LLMProvider, DocumentParseMode, LocalParser, RetrievalMethod)`
- `embedding_provider: Literal["local", "ollama"] = "local"` → `embedding_provider: EmbeddingProvider = EmbeddingProvider.LOCAL`
- `vector_backend: Literal["chroma"] = "chroma"` → `vector_backend: str = "chroma"`（单值，不枚举）
- `facts_provider: Literal["openai", "anthropic"] | None = None` → `LLMProvider | None = None`
- `chatbot_backend: Literal["langchain", "agent_sdk"] = "langchain"` → `ChatbotBackend = ChatbotBackend.LANGCHAIN`
- `workflow_backend` → 同上
- `agent_provider: Literal["openai", "anthropic"] = Field(default="openai", validation_alias="LANGCHAIN_AGENT_PROVIDER")` → `agent_provider: LLMProvider = Field(default=LLMProvider.OPENAI, validation_alias="LANGCHAIN_AGENT_PROVIDER")`
- `critic_provider: Literal["openai", "anthropic"] | None = None` → `LLMProvider | None = None`
- `document_parse_mode: Literal["local", "native", "hybrid"] = "hybrid"` → `DocumentParseMode = DocumentParseMode.HYBRID`
- `document_local_parser: Literal["pypdf", "docling"] = "pypdf"` → `LocalParser = LocalParser.PYPDF`
- `retrieval_method: Literal["vector", "bm25", "hybrid"] = "hybrid"` → `RetrievalMethod = RetrievalMethod.HYBRID`
- `rerank_provider: Literal["openai", "anthropic"] | None = None` → `LLMProvider | None = None`

- [ ] **Step 2: 运行测试**

```bash
cd backend && python -m pytest tests/test_settings_fields.py tests/test_settings_llm.py tests/test_settings_backend.py tests/test_settings_truncation.py -q
```
Expected: 全部 PASS

- [ ] **Step 3: 验证环境变量加载**

```bash
cd backend && python -c "
from porto_chatbot.settings import Settings
s = Settings()
assert s.agent_provider == 'openai', f'Expected openai, got {s.agent_provider}'
assert s.embedding_provider == 'local'
assert s.retrieval_method == 'hybrid'
print('Settings OK')
"
```
Expected: `Settings OK`

- [ ] **Step 4: Commit**

```bash
git add backend/src/porto_chatbot/settings.py
git commit -m "refactor(settings): replace Literal types with StrEnum"
```

---

### Task 4: 更新 llm 层（types, client）

**Files:**
- Modify: `backend/src/porto_chatbot/llm/types.py`
- Modify: `backend/src/porto_chatbot/llm/client.py`

**Interfaces:**
- Consumes: Task 2 的 StrEnum
- Produces: `FinishReason`（常量集）、`ContentType`（llm/types.py）；`ToolLoopResult.reason` → `TruncationReason | None`

- [ ] **Step 1: 更新 llm/types.py**

- 添加 `from enum import StrEnum`
- 添加 `from ..models.enums import TruncationReason`
- 新增 `FinishReason` 和 `ContentType` StrEnum（spec §3.3 定义）
- `ToolLoopResult.reason: str | None = None` → `ToolLoopResult.reason: TruncationReason | None = None`
- 更新 `reason` 字段的注释（值改为引用 TruncationReason 成员）

- [ ] **Step 2: 更新 llm/client.py**

- 添加 `from .types import FinishReason, ContentType` 和 `from ..models.enums import LLMProvider, ChatRole`
- `_finish_reason()`:
  - `if fr == "max_tokens":` → **保持不变**（比较外部原始值）
  - `return "length"` → `return FinishReason.LENGTH.value`
  - 返回类型签名保持 `str | None`
- `if finish_reason == "length":` → `if finish_reason == FinishReason.LENGTH:`
- `if _finish_reason(resp) != "length":` → `if _finish_reason(resp) != FinishReason.LENGTH:`
- `self.settings.agent_provider == "anthropic"` → `LLMProvider.ANTHROPIC`（4 处：行 62, 78, 408, 426）
- `self.settings.agent_provider == "openai"` → `LLMProvider.OPENAI`（3 处：行 78, 406, 424）
- `role == "system"` → `ChatRole.SYSTEM`；`role == "user"` → `ChatRole.USER`；`role == "assistant"` → `ChatRole.ASSISTANT`（行 380-384）
- `block.type == "text"` → `ContentType.TEXT`（行 121）

- [ ] **Step 3: 运行测试**

```bash
cd backend && python -m pytest tests/test_llm_client_truncation.py tests/test_llm_modern.py tests/test_llm_langchain.py tests/test_llm_timeout.py -q
```
Expected: 全部 PASS

- [ ] **Step 4: Commit**

```bash
git add backend/src/porto_chatbot/llm/
git commit -m "refactor(llm): add FinishReason/ContentType enums, replace string comparisons"
```

---

### Task 5: 更新 workflow 状态机（store, executor, api/routes/workflow）

**Files:**
- Modify: `backend/src/porto_chatbot/workflow_store.py`
- Modify: `backend/src/porto_chatbot/workflow_executor.py`
- Modify: `backend/src/porto_chatbot/api/routes/workflow.py`

**Interfaces:**
- Consumes: `WorkflowRunState` from Task 2

- [ ] **Step 1: 更新 workflow_store.py**

- 添加 `from ..models.enums import WorkflowRunState`
- `:81` `status="created"` → `WorkflowRunState.CREATED`

- [ ] **Step 2: 更新 workflow_executor.py**

- 添加 `from ..models.enums import WorkflowRunState, StepStatus, TruncationReason`
- 所有 `update_status(wid, "running")` → `update_status(wid, WorkflowRunState.RUNNING)`（约 7 处）
- 所有 `update_status(wid, "failed")` → `WorkflowRunState.FAILED`（约 3 处）
- `update_status(wid, "completed")` → `WorkflowRunState.COMPLETED`
- `update_status(wid, "awaiting_input")` → `WorkflowRunState.AWAITING_INPUT`
- `update_status(wid, "interrupted")` → `WorkflowRunState.INTERRUPTED`
- `AgentStep(status="completed"/"failed"/"running"/"pending")` → `StepStatus.*`

- [ ] **Step 3: 更新 api/routes/workflow.py**

- 添加 `from ...models.enums import WorkflowRunState, DocumentParseMode`
- Pydantic model 字段类型：`status: str` → `WorkflowRunState`（3 处 model：WorkflowCreated, WorkflowListItem, WorkflowDetail）
- `DocumentCapabilitiesView.parse_mode: str` → `DocumentParseMode`
- `status="running"` → `WorkflowRunState.RUNNING`
- `row["status"] == "completed"` → `WorkflowRunState.COMPLETED`
- 其他 status 字符串比较 → `WorkflowRunState.*`

- [ ] **Step 4: 运行测试**

```bash
cd backend && python -m pytest tests/test_workflow_api.py tests/test_workflow_executor.py tests/test_workflow_store.py tests/test_workflow_startup_recovery.py -q
```
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/workflow_store.py backend/src/porto_chatbot/workflow_executor.py backend/src/porto_chatbot/api/routes/workflow.py
git commit -m "refactor(workflow): replace status strings with WorkflowRunState enum"
```

---

### Task 6: 更新基础服务层（intent, health, embeddings, vector_store, retrieval）

**Files:**
- Modify: `backend/src/porto_chatbot/intent.py`
- Modify: `backend/src/porto_chatbot/health.py`
- Modify: `backend/src/porto_chatbot/embeddings.py`
- Modify: `backend/src/porto_chatbot/vector_store.py`
- Modify: `backend/src/porto_chatbot/retrieval.py`

- [ ] **Step 1: 更新 intent.py**

- 删除 `ChatIntent = Literal["direct", "rag"]`（行 11）
- 添加 `from .models.enums import ChatIntent`（注意：现在从 models.enums 导入，而非本地定义）
- `IntentDecision.intent: ChatIntent` 类型不变（但现在指向 StrEnum）
- `IntentDecision("direct", ...)` → `IntentDecision(ChatIntent.DIRECT, ...)`
- `IntentDecision("rag", ...)` → `IntentDecision(ChatIntent.RAG, ...)`
- `if intent not in ("direct", "rag"):` → `if intent not in [e.value for e in ChatIntent]:`
- JSON schema `"enum": ["direct", "rag"]` → `[e.value for e in ChatIntent]`

- [ ] **Step 2: 更新 health.py**

- 添加 `from .models.enums import (DependencyName, DependencyStatus, FeatureName, EmbeddingProvider, LLMProvider)`
- `name: DependencyName = "embedding"` → `DependencyName.EMBEDDING`
- `settings.embedding_provider == "local"` → `EmbeddingProvider.LOCAL`
- `name == "critic_llm"` → `DependencyName.CRITIC_LLM`
- `name == "agent_llm"` → `DependencyName.AGENT_LLM`
- `provider == "openai"` / `provider == "anthropic"` → `LLMProvider.*`
- `status="ok"/"down"/"unknown"` → `DependencyStatus.*`（所有 DependencyHealth 构造）
- `by_name.get("agent_llm")` → `by_name.get(DependencyName.AGENT_LLM)`
- `by_name.get("embedding")` → `by_name.get(DependencyName.EMBEDDING)`
- `by_name.get("agent_llm").status != "down"` → `!= DependencyStatus.DOWN`
- `by_name.get("embedding").status == "ok"` → `== DependencyStatus.OK`
- `name="chat"/"rag_search"/"workflow"` → `FeatureName.*`

- [ ] **Step 3: 更新 embeddings.py**

- 添加 `from .models.enums import EmbeddingProvider`
- `:53` `self.settings.embedding_provider == "local"` → `EmbeddingProvider.LOCAL`
- `:55` `self.settings.embedding_provider == "ollama"` → `EmbeddingProvider.OLLAMA`

- [ ] **Step 4: 更新 vector_store.py**

- 添加 `from .models.enums import RetrievalMethod, EmbeddingProvider`
- `method == "vector"` → `RetrievalMethod.VECTOR`
- `method == "bm25"` → `RetrievalMethod.BM25`
- `self.settings.embedding_provider == "local"` → `EmbeddingProvider.LOCAL`（2 处：行 360, 373）

- [ ] **Step 5: 更新 retrieval.py**

- 添加 `from .models.enums import LLMProvider`
- `:141` `provider == "openai"` → `LLMProvider.OPENAI`
- `:157` `provider == "anthropic"` → `LLMProvider.ANTHROPIC`

- [ ] **Step 6: 运行测试**

```bash
cd backend && python -m pytest tests/test_intent.py tests/test_backends.py tests/test_vector_store.py tests/test_bm25_index.py -q
```
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add backend/src/porto_chatbot/intent.py backend/src/porto_chatbot/health.py backend/src/porto_chatbot/embeddings.py backend/src/porto_chatbot/vector_store.py backend/src/porto_chatbot/retrieval.py
git commit -m "refactor: replace string literals with StrEnum in intent, health, embeddings, vector_store, retrieval"
```

---

### Task 7: 更新 documents.py

**Files:**
- Modify: `backend/src/porto_chatbot/documents.py`

- [ ] **Step 1: 新增模块内枚举 + 替换 Literal**

- 添加 `from enum import StrEnum`
- 添加 `from .models.enums import DocumentParseMode, LocalParser`（DocumentParseMode 和 LocalParser 从 models.enums 导入）
- 新增模块内 `ContentFormat`, `DocumentFormat`, `ImageKind` StrEnum（spec §3.2 定义）
- 删除本地 Literal 别名 `ContentFormat`, `DocumentFormat`, `DocumentParseMode`（行 37-39）
- `mode == "local"` → `DocumentParseMode.LOCAL`；`mode == "native"` → `DocumentParseMode.NATIVE`（行 131, 138, 153）
- `content_format == "markdown"` → `ContentFormat.MARKDOWN`（行 354）
- `local_parser == "docling"` → `LocalParser.DOCLING`（行 213）
- `parser == "pypdf"` → `LocalParser.PYPDF`（行 219）
- `DocumentArtifact.parser` → 保留 str（动态拼接）
- `suffix == ".pdf"` 等文件扩展名 → 保留 str
- `ImageKind` 的 `kind: Literal[...]` → `ImageKind` 枚举类型（行 63）

- [ ] **Step 2: 运行测试**

```bash
cd backend && python -m pytest tests/test_documents.py -q
```
Expected: 全部 PASS

- [ ] **Step 3: Commit**

```bash
git add backend/src/porto_chatbot/documents.py
git commit -m "refactor(documents): add module enums, replace Literal and string comparisons"
```

---

### Task 8: 更新 agent 层（state, heuristics, factory, agent, nodes, langchain_chat）

**Files:**
- Modify: `backend/src/porto_chatbot/agent/state.py`
- Modify: `backend/src/porto_chatbot/agent/heuristics.py`
- Modify: `backend/src/porto_chatbot/agent/factory.py`
- Modify: `backend/src/porto_chatbot/agent/agent.py`
- Modify: `backend/src/porto_chatbot/agent/langchain_chat.py`
- Modify: `backend/src/porto_chatbot/agent/nodes/understand.py`
- Modify: `backend/src/porto_chatbot/agent/nodes/` 其他节点（检查）

- [ ] **Step 1: 更新 agent/state.py**

- 新增 `BusinessDomain` StrEnum（模块内）
- `DOMAIN_HINTS` key 改为 `BusinessDomain.*`

- [ ] **Step 2: 更新 agent/heuristics.py**

- `from .state import BusinessDomain, DOMAIN_HINTS`
- `matched_domains` 返回 `dict[BusinessDomain, list[str]]`
- `responsibility_for`, `capabilities_for`, `entities_for` 参数类型改为 `BusinessDomain`，dict key 改为枚举
- `raw_type if raw_type in ("new", "extend", "existing")` → `raw_type if raw_type in [e.value for e in SubsystemType]`
- `subsystem_schema()` 的 `"enum": ["new", "extend", "existing"]` → `[e.value for e in SubsystemType]`
- `from ..models.enums import SubsystemType`

- [ ] **Step 3: 更新 agent/factory.py**

- 新增 `BackendScope` StrEnum（模块内）
- `from ..models.enums import ChatbotBackend`
- `backend_name == "agent_sdk"` → `ChatbotBackend.AGENT_SDK`
- `scope == "chatbot"` → `BackendScope.CHATBOT`

- [ ] **Step 4: 更新 agent/agent.py**

- `from .factory import BackendScope`
- `from ..models.enums import StepStatus`
- `:40` `scope="workflow"` → `scope=BackendScope.WORKFLOW`
- `:75` `AgentStep(status="completed")` → `StepStatus.COMPLETED`
- 检查其他 `status="..."` 赋值 → `StepStatus.*`

- [ ] **Step 5: 更新 agent/langchain_chat.py**

- `from ..models.enums import ChatIntent`
- `decision.intent == "direct"` → `ChatIntent.DIRECT`（2 处：行 157, 285）
- `decision.reason` → 保留 str（不改）

- [ ] **Step 6: 更新 agent/nodes/understand.py**

- `from ...models.enums import TruncationReason`（或从 llm.types 导入）
- `result.reason == "max_tokens_truncated"` → `TruncationReason.MAX_TOKENS_TRUNCATED`

- [ ] **Step 7: 检查 agent/nodes/ 其他节点**

- evaluate.py, generate.py, identify.py, retrieve.py — 搜索 `== "` 和 `status="..."`，替换为对应枚举

- [ ] **Step 8: 运行测试**

```bash
cd backend && python -m pytest tests/test_agent.py tests/test_agent_graph.py tests/test_supervisor.py tests/test_langgraph_orchestration_spike.py -q
```
Expected: 全部 PASS

- [ ] **Step 9: Commit**

```bash
git add backend/src/porto_chatbot/agent/
git commit -m "refactor(agent): add BusinessDomain/BackendScope enums, replace string literals"
```

---

### Task 9: 更新 specs 层（steps, loop, rubric）

**Files:**
- Modify: `backend/src/porto_chatbot/specs/steps.py`
- Modify: `backend/src/porto_chatbot/specs/loop.py`
- Modify: `backend/src/porto_chatbot/specs/rubric.py`

- [ ] **Step 1: 更新 specs/steps.py**

- `from ..models.enums import SpecVerdict, TruncationReason`
- `result.reason == "max_tokens_truncated"` → `TruncationReason.MAX_TOKENS_TRUNCATED`
- `:97` `verdict = parsed.get("verdict", "NEEDS_IMPROVEMENT")` → `parsed.get("verdict", SpecVerdict.NEEDS_IMPROVEMENT.value)`
- `:98-99` `if verdict not in ("PASS", "NEEDS_IMPROVEMENT", "FAIL"):` → `if verdict not in [e.value for e in SpecVerdict]:`
- `:99` `verdict = "NEEDS_IMPROVEMENT"` → `verdict = SpecVerdict.NEEDS_IMPROVEMENT.value`

- [ ] **Step 2: 更新 specs/loop.py**

- `from ..models.enums import SpecVerdict`
- `:50` `SpecAttempt(version=i, verdict="NEEDS_IMPROVEMENT", ...)` → `verdict=SpecVerdict.NEEDS_IMPROVEMENT`
- `:65` `critique.verdict == "PASS"` → `SpecVerdict.PASS`

- [ ] **Step 3: 更新 specs/rubric.py**

- `from ..models.enums import SpecVerdict`
- `:27` `"enum": ["PASS", "NEEDS_IMPROVEMENT", "FAIL"]` → `[e.value for e in SpecVerdict]`

- [ ] **Step 4: 运行测试**

```bash
cd backend && python -m pytest tests/test_spec_loop.py tests/test_spec_result_tool_meta.py tests/test_spec_loop_tool_truncation.py tests/test_evaluation.py -q
```
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/specs/
git commit -m "refactor(specs): replace verdict strings and truncation reasons with StrEnum"
```

---

### Task 10: 更新 memory + agent_sdk + api routes 剩余

**Files:**
- Modify: `backend/src/porto_chatbot/memory/facts.py`
- Modify: `backend/src/porto_chatbot/agent_sdk/backend.py`
- Modify: `backend/src/porto_chatbot/api/routes/chat.py`
- Modify: `backend/src/porto_chatbot/index_supervisor.py`
- Modify: `backend/src/porto_chatbot/tools/registry.py`（如有 string literal）

- [ ] **Step 1: 更新 memory/facts.py**

- `from ..models.enums import FactCategory, FactStatus, FactAction`
- `_CATEGORY_PRIORITY` dict key → `FactCategory.USER_DECISION` 等（4 个 key）
- `_CATEGORY_HEADERS` dict key → 同上（4 个 key）
- `:227` `action = item.get("action", "add")` → `item.get("action", FactAction.ADD.value)`
- `:230` `if action == "retract":` → `FactAction.RETRACT`
- SQL 中 `status='active'` → `f"status='{FactStatus.ACTIVE.value}'"` 或用参数化 `status=?` 传入 `FactStatus.ACTIVE.value`
- SQL 中 `status='retracted'` → 同上用 `FactStatus.RETRACTED.value`

- [ ] **Step 2: 更新 agent_sdk/backend.py**

- 新增模块内 `ClaudeMsgSubtype`, `AnthropicEventType`, `AnthropicDeltaType` StrEnum
- `from ..models.enums import StepStatus`
- `msg.subtype != "success"` → `ClaudeMsgSubtype.SUCCESS`（行 203）
- `msg.subtype == "init"` → `ClaudeMsgSubtype.INIT`（行 419, 548）
- `event.get("type") == "content_block_delta"` → `AnthropicEventType.CONTENT_BLOCK_DELTA`（行 557）
- `event.get("delta", {}).get("type") == "text_delta"` → `AnthropicDeltaType.TEXT_DELTA`（行 558）
- `AgentStep(status="completed"/"failed"/"running")` → `StepStatus.*`
- `NodeExecutionResult.reason` → 保留 str

- [ ] **Step 3: 更新 api/routes/chat.py**

- `from ...models.enums import ChatbotBackend` 或 `from .factory import BackendScope`
- `:39,60` `scope="chatbot"` → `BackendScope.CHATBOT`（需要导入 BackendScope）

- [ ] **Step 4: 更新 index_supervisor.py**

- `from ..models.enums import IndexJobState`
- `status.status == "running"` → `IndexJobState.RUNNING`（行 97）
- `rag_available()` 返回的 reason 字符串 → 保留 str

- [ ] **Step 5: 检查 tools/registry.py**

- JSON schema 中的 `"enum": [...]` → 从 StrEnum 动态生成（如有）

- [ ] **Step 6: 运行测试**

```bash
cd backend && python -m pytest tests/memory/ tests/test_chat_dispatch.py tests/test_chat_facts.py tests/api/ tests/test_node_backend_dispatch.py tests/test_agent_sdk_backend.py tests/test_agent_sdk_chat.py -q
```
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add backend/src/porto_chatbot/memory/ backend/src/porto_chatbot/agent_sdk/ backend/src/porto_chatbot/api/routes/chat.py backend/src/porto_chatbot/index_supervisor.py
git commit -m "refactor: replace string literals in memory, agent_sdk, api routes, index_supervisor"
```

---

### Task 11: 全量测试回归 + Lint

**Files:** 无修改（验证任务）

- [ ] **Step 1: 全量测试**

```bash
cd backend && python -m pytest -q
```
Expected: 全部 PASS（53 个测试文件）

- [ ] **Step 2: Lint 全量检查**

```bash
cd backend && python -m ruff check src/
```
Expected: 无错误

- [ ] **Step 3: 检查残留 string literal**

```bash
cd backend && grep -rn '== "' src/porto_chatbot/ --include="*.py" | grep -v __pycache__ | grep -v 'test_' | grep -v '# 保留' | grep -v 'suffix ==' | grep -v 'decision.reason' | grep -v '.subtype'
```
检查残留的 string 比较，确认每一条都是已知的"保留 str"情况。

- [ ] **Step 4: 验证 API JSON 输出**

```bash
cd backend && python -c "
from porto_chatbot.models import ChatMessage, AgentStep, Subsystem
msg = ChatMessage(role='user', content='test')
assert msg.role == 'user'
assert msg.model_dump()['role'] == 'user'
step = AgentStep(name='test')
assert step.status == 'pending'
assert step.model_dump()['status'] == 'pending'
print('JSON serialization OK')
"
```
Expected: `JSON serialization OK`

- [ ] **Step 5: 最终 Commit（如有修复）**

```bash
git add -A
git commit -m "refactor: final cleanup and regression verification for StrEnum migration"
```
（如果无需修复则跳过此步）
