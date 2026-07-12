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

export type WorkflowResponse = {
  workflow_id: string;
  project_name: string;
  understanding: string;
  subsystems: Subsystem[];
  specs: Record<string, string>;
  evaluation: {
    score: number;
    passed: boolean;
    checks: Array<{ name: string; passed: boolean; weight: number }>;
  };
  sources: SourceChunk[];
  steps: AgentStep[];
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
};

export type AgentConfig = {
  agent_provider: "openai" | "anthropic";
  agent_model: string;
  agent_base_url: string | null;
  agent_api_key: string | null;
  agent_temperature: number;
  agent_max_tokens: number;
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

export type KbStats = {
  kb_path: string;
  documents: number;
  chunks: number;
};

export type InspectorState = {
  steps: AgentStep[];
  sources: SourceChunk[];
  memory: SourceChunk[];
  evaluation: ChatResponseEval | null;
  workflow: WorkflowResponse | null;
};
