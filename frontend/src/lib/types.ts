export type AgentStep = {
  name: string;
  status: "pending" | "running" | "completed" | "failed";
  summary: string;
  data: Record<string, unknown>;
};

export type SourceChunk = {
  id: string;
  path: string;
  title: string;
  text: string;
  score: number;
  metadata: Record<string, unknown>;
};

export type Subsystem = {
  name: string;
  type: "new" | "extend" | "existing";
  responsibility: string;
  capabilities: string[];
  data_entities: string[];
  dependencies: string[];
};

export type ChatResponseEval = {
  score: number;
  passed: boolean;
  cases: Array<{
    question: string;
    score: number;
    passed: boolean;
    metrics: Record<string, number>;
  }>;
};

export type ChatResponse = {
  answer: string;
  sources: SourceChunk[];
  steps: AgentStep[];
  evaluation: ChatResponseEval;
  memory: SourceChunk[];
};

export type RagConfig = {
  embedding_provider: "local" | "ollama";
  embedding_model: string;
  embedding_base_url: string;
  chunk_size: number;
  chunk_overlap: number;
  top_k: number;
  kb_dirs: string[];
  retrieval_method: "vector" | "bm25" | "hybrid";
  bm25_top_k: number;
  // hybrid 融合时向量检索的权重（llama-index QueryFusionRetriever RRF），BM25 权重 = 1 - 该值
  hybrid_vector_weight: number;
  // 重排序（llama-index LLMRerank）
  rerank_enabled: boolean;
  rerank_top_n: number;
  rerank_provider: "openai" | "anthropic" | null;
  rerank_model: string | null;
  rerank_choice_batch_size: number;
};

export type AgentConfig = {
  // LLM 连接
  agent_provider: "openai" | "anthropic";
  agent_model: string;
  agent_base_url: string | null;
  agent_api_key: string | null;
  agent_temperature: number;
  agent_max_tokens: number;
  // Critic（独立评审模型，可选）
  critic_provider: "openai" | "anthropic" | null;
  critic_model: string | null;
  critic_base_url: string | null;
  critic_api_key: string | null;
  critic_temperature: number | null;
  critic_max_tokens: number | null;
  // Spec refine loop
  spec_refine_enabled: boolean;
  spec_refine_max_iter: number;
  spec_refine_concurrency: number;
  spec_refine_pass_score: number;
  spec_refine_budget_tokens: number;
  // Workflow 条件回边
  workflow_rework_enabled: boolean;
  workflow_rework_max_passes: number;
  // Memory compaction
  memory_compact_threshold: number;
  memory_recent_keep: number;
  // Context / streaming / tool
  context_char_budget: number;
  agent_stream_enabled: boolean;
  agent_max_tool_turns: number;
  // HTTP request timeout (seconds) for agent LLM calls
  agent_request_timeout: number;
};

export type AppSettings = {
  rag: RagConfig;
  agent: AgentConfig;
};

export type MemoryRecord = {
  id: string;
  session_id: string;
  role: string;
  content: string;
  created_at: string;
};

export type IndexJobStatus = {
  status: "idle" | "running" | "succeeded" | "failed" | "interrupted";
  source: string | null;
  reset: boolean;
  progress_done: number;
  progress_total: number;
  chunks_done: number;
  started_at: string | null;
  heartbeat_at: string | null;
  finished_at: string | null;
  last_indexed_at: string | null;
  last_stats: KbStats | null;
  error: string | null;
};

export type KbStats = {
  kb_path: string;
  documents: number;
  chunks: number;
  embedding_provider?: string;
  embedding_model?: string;
  embedding_dimensions?: number | null;
  chunk_size?: number;
  chunk_overlap?: number;
  rag_index?: IndexJobStatus;
};

export type DependencyName = "embedding" | "agent_llm" | "critic_llm";
export type DependencyStatus = "ok" | "degraded" | "down" | "unknown";
export type DependencyHealth = {
  name: DependencyName;
  status: DependencyStatus;
  latency_ms: number | null;
  detail: string | null;
  checked_at: string | null;
};

export type FeatureName = "chat" | "rag_search" | "workflow";
export type FeatureAvailability = {
  name: FeatureName;
  available: boolean;
  reason: string | null;
};

export type HealthSnapshot = {
  dependencies: DependencyHealth[];
  features: FeatureAvailability[];
  rag_index: IndexJobStatus;
  updated_at: string | null;
};

export type InspectorState = {
  steps: AgentStep[];
  sources: SourceChunk[];
  memory: SourceChunk[];
  evaluation: ChatResponseEval | null;
};

export type WorkflowStepName =
  | "retrieve"
  | "understand"
  | "identify"
  | "generate"
  | "evaluate";

export type WorkflowStatus =
  | "created"
  | "running"
  | "awaiting_input"
  | "completed"
  | "failed"
  | "interrupted";

export type WorkflowOutputEntry = {
  output: Record<string, unknown>;
  produced_by: "ai" | "user";
  produced_at: string;
};

export type WorkflowDetail = {
  workflow_id: string;
  session_id: string;
  project_name: string | null;
  status: WorkflowStatus;
  current_step: WorkflowStepName | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  outputs: Partial<Record<WorkflowStepName, WorkflowOutputEntry>>;
};

export type WorkflowListItem = {
  workflow_id: string;
  project_name: string | null;
  status: WorkflowStatus;
  current_step: WorkflowStepName | null;
  created_at: string;
  score: number | null;
};
