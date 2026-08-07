from __future__ import annotations

from .chat import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    MemorySearchResponse,
    MessageRecord,
    SessionFact,
)
from .common import (
    DependencyHealth,
    FeatureAvailability,
    HealthSnapshot,
    IndexJobStatus,
    IndexStats,
    IndexStatsView,
    SourceChunk,
)
from .enums import (
    ChatbotBackend,
    ChatIntent,
    ChatRole,
    DependencyName,
    DependencyStatus,
    DocumentParseMode,
    EmbeddingProvider,
    FactAction,
    FactCategory,
    FactStatus,
    FeatureName,
    IndexJobState,
    LLMProvider,
    LocalParser,
    RetrievalMethod,
    SpecVerdict,
    StepStatus,
    SubsystemType,
    TruncationReason,
    WorkflowRunState,
)
from .evaluation import (
    RagBatchEvaluation,
    RagCaseEvaluation,
    RagMetrics,
    WorkflowCheck,
    WorkflowEvaluation,
)
from .payload import (
    AgentSettingsPayload,
    AppSettingsPayload,
    AppSettingsResponse,
    DocumentSettingsPayload,
    EvalCase,
    EvalRequest,
    IndexRequest,
    RagChatSettingsPayload,
    RagSettingsPayload,
    RagWorkflowSettingsPayload,
    WorkflowRequest,
)
from .file import FileHit, FileMeta, FileInfo
from .spec import Critique, SpecAttempt, SpecResult
from .workflow import AgentStep, Subsystem, WorkflowResponse

__all__ = [
    "SourceChunk",
    "IndexStats",
    "IndexJobStatus",
    "IndexStatsView",
    "DependencyHealth",
    "DependencyName",
    "DependencyStatus",
    "FeatureAvailability",
    "FeatureName",
    "HealthSnapshot",
    "RagSettingsPayload",
    "RagChatSettingsPayload",
    "RagWorkflowSettingsPayload",
    "AgentSettingsPayload",
    "AppSettingsPayload",
    "AppSettingsResponse",
    "DocumentSettingsPayload",
    "IndexRequest",
    "WorkflowRequest",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "MemoryRecord",  # deprecated alias of MessageRecord (Task 10 removes)
    "MessageRecord",
    "MemorySearchResponse",
    "SessionFact",
    "EvalCase",
    "EvalRequest",
    "Subsystem",
    "WorkflowResponse",
    "AgentStep",
    "Critique",
    "SpecAttempt",
    "SpecResult",
    # ── Evaluation models ──
    "WorkflowCheck",
    "WorkflowEvaluation",
    "RagMetrics",
    "RagCaseEvaluation",
    "RagBatchEvaluation",
    # ── File models ──
    "FileMeta",
    "FileInfo",
    "FileHit",
    # ── Enums (20) ──
    "ChatbotBackend",
    "ChatIntent",
    "ChatRole",
    "DocumentParseMode",
    "EmbeddingProvider",
    "FactAction",
    "FactCategory",
    "FactStatus",
    "IndexJobState",
    "LLMProvider",
    "LocalParser",
    "RetrievalMethod",
    "SpecVerdict",
    "StepStatus",
    "SubsystemType",
    "TruncationReason",
    "WorkflowRunState",
]


# Backward-compat alias: existing imports of `MemoryRecord` keep working
# during the session/message/memory split. Removed in Task 10.
MemoryRecord = MessageRecord  # noqa: F811  (deprecated alias)
