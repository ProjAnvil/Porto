import type {
  AgentConfig,
  AppSettings,
  ChatResponse,
  KbStats,
  MemoryRecord,
  RagConfig,
  SourceChunk,
  WorkflowResponse,
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

export async function indexKnowledgeBase(config: RagConfig) {
  return parseJson<KbStats>(
    await fetch("/api/kb/index", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...config, reset: true }),
    }),
  );
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

export async function runWorkflow(
  text: string,
  projectName: string | undefined,
  sessionId: string,
  config: RagConfig,
  agent?: AgentConfig,
): Promise<WorkflowResponse> {
  return parseJson<WorkflowResponse>(
    await fetch("/api/porto/workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        project_name: projectName || undefined,
        session_id: sessionId,
        rag: config,
        agent,
        top_k: config.top_k,
      }),
    }),
  );
}

export async function runWorkflowUpload(file: File, projectName?: string) {
  const form = new FormData();
  form.set("file", file);
  if (projectName) form.set("project_name", projectName);
  return parseJson<WorkflowResponse>(
    await fetch("/api/porto/workflows/upload", {
      method: "POST",
      body: form,
    }),
  );
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
};

export const defaultAgentConfig: AgentConfig = {
  agent_provider: "openai",
  agent_model: "gpt-4.1-mini",
  agent_base_url: "",
  agent_api_key: "",
  agent_temperature: 0.2,
  agent_max_tokens: 2000,
};
