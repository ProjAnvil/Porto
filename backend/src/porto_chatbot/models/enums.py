"""全局共享 StrEnum 定义。

所有枚举成员值与重构前的字符串字面量完全匹配（大小写敏感），
StrEnum 是 str 的子类，``==`` 比较、JSON 序列化、字典 key 哈希
均与原字符串完全兼容。
"""

from __future__ import annotations

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
