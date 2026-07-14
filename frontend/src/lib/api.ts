import type {
  AgentConfig,
  AppSettings,
  ChatResponse,
  HealthSnapshot,
  IndexJobStatus,
  KbStats,
  MemoryRecord,
  Paginated,
  RagConfig,
  SessionItem,
  SourceChunk,
  WorkflowDetail,
  WorkflowListItem,
  WorkflowStepName,
} from "./types";

async function parseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return (await response.json()) as T;
}

export async function getKbStats() {
  return parseJson<KbStats>(await fetch("/api/kb/stats"));
}

export async function getAppSettings() {
  return parseJson<AppSettings>(await fetch("/api/settings"));
}

export async function saveAppSettings(settings: {
  rag?: Partial<RagConfig>;
  agent?: Partial<AgentConfig>;
}) {
  return parseJson<AppSettings>(
    await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    }),
  );
}

export async function indexKnowledgeBase(config: RagConfig): Promise<IndexJobStatus> {
  return parseJson<IndexJobStatus>(
    await fetch("/api/kb/index", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...config, reset: true }),
    }),
  );
}

export async function getHealth(): Promise<HealthSnapshot> {
  return parseJson<HealthSnapshot>(await fetch("/api/health"));
}

export async function sendChat(
  message: string,
  sessionId: string,
  config: RagConfig,
): Promise<ChatResponse> {
  return parseJson<ChatResponse>(
    await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        session_id: sessionId,
        rag: config,
        top_k: config.top_k,
      }),
    }),
  );
}

export async function createWorkflow(body: {
  text: string;
  project_name?: string;
  session_id: string;
  rag?: RagConfig;
  agent?: AgentConfig;
  top_k?: number;
}): Promise<{ workflow_id: string; status: string }> {
  return parseJson<{ workflow_id: string; status: string }>(
    await fetch("/api/porto/workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function createWorkflowUpload(
  file: File,
  projectName?: string,
): Promise<{ workflow_id: string; status: string }> {
  const form = new FormData();
  form.set("file", file);
  if (projectName) form.set("project_name", projectName);
  return parseJson<{ workflow_id: string; status: string }>(
    await fetch("/api/porto/workflows/upload", {
      method: "POST",
      body: form,
    }),
  );
}

export async function listWorkflows(params?: {
  sessionId?: string;
  date?: string;
  limit?: number;
  offset?: number;
}) {
  const q = new URLSearchParams();
  const p = params ?? {};
  if (p.sessionId) q.set("session_id", p.sessionId);
  if (p.date) q.set("date", p.date);
  q.set("limit", String(p.limit ?? 20));
  q.set("offset", String(p.offset ?? 0));
  return parseJson<Paginated<WorkflowListItem>>(
    await fetch(`/api/porto/workflows?${q.toString()}`),
  );
}

export async function listSessions(date?: string, limit = 20, offset = 0) {
  const q = new URLSearchParams();
  if (date) q.set("date", date);
  q.set("limit", String(limit));
  q.set("offset", String(offset));
  return parseJson<Paginated<SessionItem>>(
    await fetch(`/api/sessions?${q.toString()}`),
  );
}

export async function getWorkflow(id: string) {
  return parseJson<WorkflowDetail>(
    await fetch(`/api/porto/workflows/${encodeURIComponent(id)}`),
  );
}

export async function advanceWorkflow(id: string) {
  return parseJson<{ workflow_id: string; status: string }>(
    await fetch(`/api/porto/workflows/${encodeURIComponent(id)}/advance`, {
      method: "POST",
    }),
  );
}

export async function saveStepOutput(
  id: string,
  step: WorkflowStepName,
  output: Record<string, unknown>,
) {
  return parseJson<WorkflowDetail>(
    await fetch(`/api/porto/workflows/${encodeURIComponent(id)}/steps/${step}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(output),
    }),
  );
}

export async function deleteWorkflow(id: string) {
  const r = await fetch(`/api/porto/workflows/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error(await r.text());
}

export async function listMemory(sessionId: string) {
  return parseJson<{ session_id: string; items: MemoryRecord[] }>(
    await fetch(`/api/memory/${encodeURIComponent(sessionId)}`),
  );
}

export async function searchMemory(query: string, sessionId: string) {
  return parseJson<{ query: string; results: SourceChunk[] }>(
    await fetch(
      `/api/memory/search?q=${encodeURIComponent(query)}&session_id=${encodeURIComponent(sessionId)}`,
    ),
  );
}

export const defaultRagConfig: RagConfig = {
  embedding_provider: "ollama",
  embedding_model: "qwen3-embedding:0.6b",
  embedding_base_url: "http://127.0.0.1:11434",
  chunk_size: 1400,
  chunk_overlap: 180,
  top_k: 6,
  kb_dirs: ["~/.scv/analysis"],
  retrieval_method: "hybrid",
  bm25_top_k: 20,
  hybrid_vector_weight: 0.5,
  rerank_enabled: false,
  rerank_top_n: 5,
  rerank_provider: null,
  rerank_model: null,
  rerank_choice_batch_size: 5,
};

export const defaultAgentConfig: AgentConfig = {
  agent_provider: "openai",
  agent_model: "gpt-4.1-mini",
  agent_base_url: "",
  agent_api_key: "",
  agent_temperature: 0.2,
  agent_max_tokens: 2000,
  critic_provider: null,
  critic_model: null,
  critic_base_url: null,
  critic_api_key: null,
  critic_temperature: null,
  critic_max_tokens: null,
  spec_refine_enabled: true,
  spec_refine_max_iter: 3,
  spec_refine_concurrency: 3,
  spec_refine_pass_score: 10,
  spec_refine_budget_tokens: 40000,
  workflow_rework_enabled: true,
  workflow_rework_max_passes: 1,
  memory_compact_threshold: 20,
  memory_recent_keep: 8,
  context_char_budget: 16000,
  agent_stream_enabled: true,
  agent_max_tool_turns: 4,
  agent_request_timeout: 120,
};
