from __future__ import annotations

from pydantic import BaseModel, Field

from .enums import (
    ChatbotBackend,
    DocumentParseMode,
    EmbeddingProvider,
    IntentRoutingMode,
    LLMProvider,
    LocalParser,
    QueryTransformStrategy,
    RerankType,
    RetrievalMethod,
)


class RagSettingsPayload(BaseModel):
    embedding_provider: EmbeddingProvider | None = None
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    chunk_size: int | None = Field(default=None, ge=200, le=8000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=2000)
    top_k: int | None = Field(default=None, ge=1, le=30)
    kb_dirs: list[str] | None = None
    retrieval_method: RetrievalMethod | None = None
    bm25_top_k: int | None = Field(default=None, ge=1, le=100)
    hybrid_vector_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    rerank_enabled: bool | None = None
    rerank_top_n: int | None = Field(default=None, ge=1, le=50)
    rerank_provider: LLMProvider | None = None
    rerank_model: str | None = None
    rerank_choice_batch_size: int | None = Field(default=None, ge=1, le=20)
    embedding_api_key: str | None = None
    rerank_type: RerankType | None = None
    rerank_api_key: str | None = None
    rerank_base_url: str | None = None


class AgentSettingsPayload(BaseModel):
    # --- Agent 引擎选择 ---
    chatbot_backend: ChatbotBackend | None = None
    workflow_backend: ChatbotBackend | None = None
    # LLM 连接
    agent_provider: LLMProvider | None = None
    agent_model: str | None = None
    agent_base_url: str | None = None
    agent_api_key: str | None = None
    agent_temperature: float | None = Field(default=None, ge=0, le=2)
    agent_max_tokens: int | None = Field(default=None, ge=1, le=128000)
    # Critic（独立评审模型，可选；未配则复用上面的 agent_*）
    critic_provider: LLMProvider | None = None
    critic_model: str | None = None
    critic_base_url: str | None = None
    critic_api_key: str | None = None
    critic_temperature: float | None = Field(default=None, ge=0, le=2)
    critic_max_tokens: int | None = Field(default=None, ge=1, le=128000)
    # Spec refine loop
    spec_refine_enabled: bool | None = None
    spec_refine_max_iter: int | None = Field(default=None, ge=0, le=10)
    spec_refine_concurrency: int | None = Field(default=None, ge=1, le=10)
    spec_refine_pass_score: int | None = Field(default=None, ge=0, le=12)
    spec_refine_budget_tokens: int | None = Field(default=None, ge=1000)
    agent_request_timeout: int | None = Field(default=None, ge=10)
    # Workflow 条件回边
    workflow_rework_enabled: bool | None = None
    workflow_rework_max_passes: int | None = Field(default=None, ge=0, le=5)
    # Memory compaction
    memory_compact_threshold: int | None = Field(default=None, ge=4)
    memory_recent_keep: int | None = Field(default=None, ge=1)
    # Session facts (a2)
    facts_enabled: bool | None = None
    facts_max_per_category: int | None = Field(default=None, ge=1, le=100)
    facts_similarity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    facts_recent_context_turns: int | None = Field(default=None, ge=1, le=20)
    # Context 预算 / 流式 / 节点 tool 轮数
    context_char_budget: int | None = Field(default=None, ge=1000)
    agent_stream_enabled: bool | None = None
    agent_max_tool_turns: int | None = Field(default=None, ge=1, le=20)


class DocumentSettingsPayload(BaseModel):
    parse_mode: DocumentParseMode | None = None
    local_parser: LocalParser | None = None
    max_tokens: int | None = Field(default=None, ge=1000, le=128000)
    max_upload_mb: int | None = Field(default=None, ge=1, le=200)
    max_pdf_pages: int | None = Field(default=None, ge=1, le=1000)


class RagChatSettingsPayload(BaseModel):
    intent_routing_mode: IntentRoutingMode | None = None
    query_transform_strategy: QueryTransformStrategy | None = None
    multi_query_count: int | None = Field(default=None, ge=2, le=8)
    hyde_fallback_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class RagWorkflowSettingsPayload(BaseModel):
    query_transform_strategy: QueryTransformStrategy | None = None
    multi_query_count: int | None = Field(default=None, ge=2, le=8)


class AppSettingsPayload(BaseModel):
    rag: RagSettingsPayload | None = None
    agent: AgentSettingsPayload | None = None
    document: DocumentSettingsPayload | None = None
    rag_chat: RagChatSettingsPayload | None = None
    rag_workflow: RagWorkflowSettingsPayload | None = None


class AppSettingsResponse(BaseModel):
    rag: RagSettingsPayload
    agent: AgentSettingsPayload
    document: DocumentSettingsPayload
    rag_chat: RagChatSettingsPayload
    rag_workflow: RagWorkflowSettingsPayload


class IndexRequest(RagSettingsPayload):
    reset: bool = True


class WorkflowRequest(BaseModel):
    text: str | None = None
    project_name: str | None = None
    top_k: int | None = None
    session_id: str = "default"
    rag: RagSettingsPayload | None = None
    agent: AgentSettingsPayload | None = None


class EvalCase(BaseModel):
    question: str
    answer: str
    contexts: list[str] = Field(default_factory=list)
    ground_truth: str | None = None


class EvalRequest(BaseModel):
    cases: list[EvalCase]
