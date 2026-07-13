"use client";

import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useMessage,
} from "@assistant-ui/react";
import {
  AssistantChatTransport,
  useChatRuntime,
} from "@assistant-ui/react-ai-sdk";
import {
  Bot,
  Braces,
  CheckCircle2,
  ChevronDown,
  Database,
  FileInput,
  Gauge,
  History,
  Loader2,
  Play,
  Plus,
  Search,
  Send,
  Settings,
  Trash2,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import {
  advanceWorkflow,
  createWorkflow,
  createWorkflowUpload,
  defaultAgentConfig,
  defaultRagConfig,
  getAppSettings,
  getHealth,
  getKbStats,
  getWorkflow,
  indexKnowledgeBase,
  listMemory,
  listWorkflows,
  saveAppSettings,
  saveStepOutput,
  searchMemory,
} from "@/lib/api";
import type {
  AgentConfig,
  ChatResponseEval,
  HealthSnapshot,
  InspectorState,
  KbStats,
  MemoryRecord,
  RagConfig,
  SourceChunk,
  Subsystem,
  WorkflowDetail,
  WorkflowListItem,
  WorkflowStepName,
} from "@/lib/types";

type Mode = "chat" | "workflow";
type View = "workbench" | "settings";
type SettingsSection = "rag" | "agent" | "knowledge";

const emptyInspector: InspectorState = {
  steps: [],
  sources: [],
  memory: [],
  evaluation: null,
};

const WORKFLOW_STEPS: WorkflowStepName[] = [
  "retrieve",
  "understand",
  "identify",
  "generate",
  "evaluate",
];

const CHECKPOINT_STEPS: WorkflowStepName[] = [
  "understand",
  "identify",
  "generate",
];

const STEP_LABELS: Record<WorkflowStepName, string> = {
  retrieve: "检索",
  understand: "理解",
  identify: "子系统",
  generate: "规格",
  evaluate: "评估",
};

function scoreClass(score?: number) {
  if (score == null) return "bg-zinc-100 text-zinc-600";
  if (score >= 0.75) return "bg-emerald-100 text-emerald-700";
  if (score >= 0.45) return "bg-amber-100 text-amber-700";
  return "bg-rose-100 text-rose-700";
}

function subsystemClass(subsystem: Subsystem) {
  if (subsystem.type === "existing") return "bg-emerald-100 text-emerald-700";
  if (subsystem.type === "extend") return "bg-amber-100 text-amber-700";
  return "bg-sky-100 text-sky-700";
}

function isInspectorState(value: unknown): value is InspectorState {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<InspectorState>;
  return (
    Array.isArray(candidate.steps) &&
    Array.isArray(candidate.sources) &&
    Array.isArray(candidate.memory)
  );
}

/**
 * 把后端某一步的 outputs 转成 CheckpointEditor 可编辑的草稿字符串。
 * - understand / generate：取 understanding / specs（specs 序列化为 markdown 文本）
 * - identify：草稿无意义（子系统卡片走 Subsystem[]），返回空串
 */
function readStepDraft(detail: WorkflowDetail, step: WorkflowStepName): string {
  const entry = detail.outputs[step];
  if (!entry) return "";
  const output = entry.output;
  if (step === "understand") {
    const text = output.understanding;
    return typeof text === "string" ? text : "";
  }
  if (step === "generate") {
    const specs = output.specs;
    if (specs && typeof specs === "object") {
      // 将 Record<string,string> 序列化为 "## name\n\nbody" 形式，方便用户整体编辑
      return Object.entries(specs as Record<string, string>)
        .map(([name, body]) => `## ${name}\n\n${body}`)
        .join("\n\n---\n\n");
    }
    return "";
  }
  return "";
}

/** 解析 generate 步骤的 markdown 草稿（## name 切分）回 specs dict */
function parseSpecsDraft(draft: string): Record<string, string> {
  const result: Record<string, string> = {};
  const blocks = draft.split(/^---$/m);
  for (const block of blocks) {
    const match = block.match(/^##\s+(.+?)\s*\n([\s\S]*)$/);
    if (match) {
      const name = match[1].trim();
      const body = match[2].trim();
      if (name) result[name] = body;
    }
  }
  return result;
}

/** 读取 detail.identify 步骤的 subsystems 列表 */
function readSubsystems(detail: WorkflowDetail): Subsystem[] {
  const entry = detail.outputs.identify;
  if (!entry) return [];
  const subs = entry.output.subsystems;
  if (!Array.isArray(subs)) return [];
  return subs as Subsystem[];
}

const EMPTY_SUBSYSTEM: Subsystem = {
  name: "",
  type: "new",
  responsibility: "",
  capabilities: [],
  data_entities: [],
  dependencies: [],
};

export function PortoWorkbench() {
  const [view, setView] = useState<View>("workbench");
  const [mode, setMode] = useState<Mode>("chat");
  const [sessionId, setSessionId] = useState(
    `porto-${new Date().toISOString().slice(0, 10)}`,
  );
  const [ragConfig, setRagConfig] = useState<RagConfig>(defaultRagConfig);
  const [agentConfig, setAgentConfig] = useState<AgentConfig>(defaultAgentConfig);
  const [kbStats, setKbStats] = useState<KbStats | null>(null);
  const [health, setHealth] = useState<HealthSnapshot | null>(null);
  const [memoryItems, setMemoryItems] = useState<MemoryRecord[]>([]);
  const [memoryQuery, setMemoryQuery] = useState("");
  const [inspector, setInspector] = useState<InspectorState>(emptyInspector);
  const [projectName, setProjectName] = useState("");
  const [workflowText, setWorkflowText] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [busyLabel, setBusyLabel] = useState("");
  const [error, setError] = useState("");
  const [workflowId, setWorkflowId] = useState<string | null>(null);
  const [workflowDetail, setWorkflowDetail] = useState<WorkflowDetail | null>(null);
  const [workflowList, setWorkflowList] = useState<WorkflowListItem[]>([]);
  const [draft, setDraft] = useState<string>("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const refreshMemory = useCallback(async () => {
    const data = await listMemory(sessionId);
    setMemoryItems(data.items);
  }, [sessionId]);

  const refreshWorkflowList = useCallback(async () => {
    try {
      const result = await listWorkflows(sessionId);
      setWorkflowList(result.items);
    } catch {
      /* 历史列表非关键，失败静默 */
    }
  }, [sessionId]);

  // 工作流历史：sessionId 变化时拉取一次
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const result = await listWorkflows(sessionId);
        if (!cancelled) setWorkflowList(result.items);
      } catch {
        /* 历史列表非关键，失败静默 */
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // 工作流详情轮询：仅在 workflowId 存在时启动；status 回到 running 时（如 advance 后）
  // 通过 status 依赖重启轮询。状态稳定为 running 期间靠 setTimeout 自驱动，不会重复触发。
  useEffect(() => {
    if (!workflowId) return;
    const id = workflowId;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const detail = await getWorkflow(id);
        if (!active) return;
        setWorkflowDetail(detail);
        // 进入 awaiting_input 或 completed 后同步草稿
        if (
          detail.status === "awaiting_input" &&
          detail.current_step &&
          CHECKPOINT_STEPS.includes(detail.current_step)
        ) {
          setDraft(readStepDraft(detail, detail.current_step));
        }
        if (detail.status === "running") {
          timer = setTimeout(poll, 2000);
        } else {
          // 终态时刷新历史列表
          void refreshWorkflowList();
        }
      } catch {
        if (active) timer = setTimeout(poll, 2000);
      }
    }
    poll();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [workflowId, workflowDetail?.status, refreshWorkflowList]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const [settingsResult, statsResult, memoryResult] = await Promise.allSettled([
        getAppSettings(),
        getKbStats(),
        listMemory(sessionId),
      ]);
      if (cancelled) return;
      if (settingsResult.status === "fulfilled") {
        setRagConfig(settingsResult.value.rag);
        setAgentConfig(settingsResult.value.agent);
      }
      setKbStats(statsResult.status === "fulfilled" ? statsResult.value : null);
      if (memoryResult.status === "fulfilled") {
        setMemoryItems(memoryResult.value.items);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // 健康面板：周期轮询 /api/health（依赖级 + 功能级可用度）
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const snap = await getHealth();
        if (active) setHealth(snap);
      } catch {
        /* 健康面板非关键，失败静默 */
      }
      if (active) timer = setTimeout(poll, 15000);
    }
    poll();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, []);

  const transport = useMemo(
    () =>
      new AssistantChatTransport({
        api: "/api/chat/stream",
        body: {
          session_id: sessionId,
          rag: ragConfig,
          agent: agentConfig,
          top_k: ragConfig.top_k,
        },
        fetch(input, init) {
          setBusyLabel("生成回答");
          setError("");
          setInspector(emptyInspector);
          return globalThis.fetch(input, init);
        },
      }),
    [agentConfig, ragConfig, sessionId],
  );

  const runtime = useChatRuntime({
    transport,
    messages: [
      {
        id: "porto-welcome",
        role: "assistant",
        parts: [
          {
            type: "text",
            text: "选择知识库问答或 Porto PRD 拆解模式。问答会检索知识库和会话记忆；拆解模式会输出子系统、规格和评估结果。",
          },
        ],
      },
    ],
    onData(dataPart) {
      if (dataPart.type !== "data-porto") return;
      if (isInspectorState(dataPart.data)) {
        setInspector(dataPart.data);
      }
    },
    onError(err) {
      setError(err instanceof Error ? err.message : "请求失败");
      setBusyLabel("");
    },
    onFinish() {
      setBusyLabel("");
      void refreshMemory();
    },
  });

  async function refreshIndex(nextConfig: RagConfig = ragConfig) {
    setError("");
    setBusyLabel("索引知识库");
    try {
      const submitted = await indexKnowledgeBase(nextConfig);
      if (submitted.status !== "running") {
        setKbStats(await getKbStats());
        return;
      }
      // 异步 reindex：轮询 /api/kb/stats 直到任务结束，期间实时展示进度
      const deadline = Date.now() + 180_000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1500));
        const stats = await getKbStats();
        setKbStats(stats);
        const st = stats.rag_index?.status;
        if (st && st !== "running") {
          if (st === "failed" || st === "interrupted") {
            setError(stats.rag_index?.error || `索引未完成（${st}）`);
          }
          break;
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "知识库索引失败");
    } finally {
      setBusyLabel("");
    }
  }

  async function saveRagConfig(nextConfig: RagConfig): Promise<RagConfig | null> {
    setBusyLabel("保存 RAG 设置");
    setError("");
    try {
      const saved = await saveAppSettings({ rag: nextConfig });
      setRagConfig(saved.rag);
      return saved.rag;
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存 RAG 设置失败");
      return null;
    } finally {
      setBusyLabel("");
    }
  }

  async function saveAgentConfig(nextConfig: AgentConfig) {
    setBusyLabel("保存 Agent 设置");
    setError("");
    try {
      const saved = await saveAppSettings({ agent: nextConfig });
      setAgentConfig(saved.agent);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存 Agent 设置失败");
    } finally {
      setBusyLabel("");
    }
  }

  async function runMemorySearch() {
    if (!memoryQuery.trim()) return;
    setBusyLabel("搜索记忆");
    setError("");
    try {
      const data = await searchMemory(memoryQuery.trim(), sessionId);
      setInspector((current) => ({ ...current, memory: data.results }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "记忆搜索失败");
    } finally {
      setBusyLabel("");
    }
  }

  async function runWorkflowAction() {
    if (!workflowText.trim() && !selectedFile) return;
    setBusyLabel("提交拆解");
    setError("");
    setWorkflowDetail(null);
    try {
      const resp = selectedFile
        ? await createWorkflowUpload(selectedFile, projectName.trim() || undefined)
        : await createWorkflow({
            text: workflowText.trim(),
            project_name: projectName.trim() || undefined,
            session_id: sessionId,
            rag: ragConfig,
            agent: agentConfig,
            top_k: ragConfig.top_k,
          });
      setWorkflowId(resp.workflow_id);
      setDraft("");
      void refreshWorkflowList();
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交拆解失败");
    } finally {
      setBusyLabel("");
    }
  }

  async function onAdvance() {
    if (!workflowId) return;
    setBusyLabel("推进");
    setError("");
    try {
      const resp = await advanceWorkflow(workflowId);
      // advance 通常会把状态切回 running，立即拉一次详情触发轮询分支
      const detail = await getWorkflow(resp.workflow_id);
      setWorkflowDetail(detail);
      if (
        detail.status === "awaiting_input" &&
        detail.current_step &&
        CHECKPOINT_STEPS.includes(detail.current_step)
      ) {
        setDraft(readStepDraft(detail, detail.current_step));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "推进失败");
    } finally {
      setBusyLabel("");
    }
  }

  async function onSaveStep(step: WorkflowStepName, output: Record<string, unknown>) {
    if (!workflowId) return;
    setBusyLabel("保存");
    setError("");
    try {
      const detail = await saveStepOutput(workflowId, step, output);
      setWorkflowDetail(detail);
      setDraft(readStepDraft(detail, step));
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存步骤产出失败");
    } finally {
      setBusyLabel("");
    }
  }

  async function onPickWorkflow(id: string) {
    setMode("workflow");
    setView("workbench");
    setWorkflowId(id);
    setWorkflowDetail(null);
    setDraft("");
    setError("");
    try {
      const detail = await getWorkflow(id);
      setWorkflowDetail(detail);
      if (
        detail.status === "awaiting_input" &&
        detail.current_step &&
        CHECKPOINT_STEPS.includes(detail.current_step)
      ) {
        setDraft(readStepDraft(detail, detail.current_step));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载工作流失败");
    }
  }

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="grid min-h-screen grid-cols-1 bg-zinc-100 text-zinc-950 lg:grid-cols-[300px_minmax(0,1fr)_380px]">
        <Sidebar
          busy={Boolean(busyLabel)}
          kbStats={kbStats}
          memoryItems={memoryItems}
          memoryQuery={memoryQuery}
          mode={mode}
          onPickWorkflow={onPickWorkflow}
          onRunMemorySearch={runMemorySearch}
          sessionId={sessionId}
          view={view}
          setMemoryQuery={setMemoryQuery}
          setMode={setMode}
          setSessionId={setSessionId}
          setView={setView}
          workflowId={workflowId}
          workflows={workflowList}
        />

        <main className="flex min-h-[70vh] min-w-0 flex-col border-x border-zinc-200 bg-white">
          <header className="flex h-14 items-center justify-between border-b border-zinc-200 px-4">
            <div className="flex items-center gap-2 text-sm font-semibold">
              {view === "settings" ? (
                <>
                  <Settings size={17} />
                  Settings
                </>
              ) : mode === "chat" ? (
                <>
                  <Search size={17} />
                  知识库问答
                </>
              ) : (
                <>
                  <Braces size={17} />
                  Porto PRD 拆解
                </>
              )}
            </div>
            {busyLabel ? (
              <span className="flex items-center gap-2 text-xs text-zinc-500">
                <Loader2 size={14} className="animate-spin" />
                {busyLabel}
              </span>
            ) : null}
          </header>

          {view === "settings" ? (
            <SettingsPage
              agentConfig={agentConfig}
              busy={Boolean(busyLabel)}
              error={error}
              health={health}
              kbStats={kbStats}
              ragConfig={ragConfig}
              onRefreshIndex={refreshIndex}
              onSaveAgent={saveAgentConfig}
              onSaveRag={saveRagConfig}
            />
          ) : mode === "chat" ? (
            <ThreadView error={error} />
          ) : (
            <WorkflowPanel
              busy={Boolean(busyLabel)}
              detail={workflowDetail}
              draft={draft}
              error={error}
              fileInputRef={fileInputRef}
              onAdvance={onAdvance}
              onRun={runWorkflowAction}
              onSaveStep={onSaveStep}
              onTextChange={setWorkflowText}
              projectName={projectName}
              selectedFile={selectedFile}
              setDraft={setDraft}
              setProjectName={setProjectName}
              setSelectedFile={setSelectedFile}
              text={workflowText}
            />
          )}
        </main>

        <Inspector inspector={inspector} memoryItems={memoryItems} />
      </div>
    </AssistantRuntimeProvider>
  );
}

function Sidebar({
  busy,
  kbStats,
  memoryItems,
  memoryQuery,
  mode,
  onPickWorkflow,
  onRunMemorySearch,
  sessionId,
  view,
  setMemoryQuery,
  setMode,
  setSessionId,
  setView,
  workflowId,
  workflows,
}: {
  busy: boolean;
  kbStats: KbStats | null;
  memoryItems: MemoryRecord[];
  memoryQuery: string;
  mode: Mode;
  onPickWorkflow: (id: string) => void;
  sessionId: string;
  view: View;
  setMemoryQuery: (value: string) => void;
  setMode: (value: Mode) => void;
  setSessionId: (value: string) => void;
  setView: (value: View) => void;
  workflowId: string | null;
  workflows: WorkflowListItem[];
  onRunMemorySearch: () => void;
}) {
  return (
    <aside className="min-w-0 border-b border-zinc-200 bg-zinc-50 p-4 lg:border-b-0">
      <div className="mb-6 flex items-center gap-3">
        <div className="flex size-9 items-center justify-center rounded-lg bg-zinc-950 text-white">
          <Bot size={19} />
        </div>
        <div>
          <h1 className="text-base font-semibold">Porto Agent</h1>
          <p className="text-xs text-zinc-500">Next + assistant-ui</p>
        </div>
      </div>

      <div className="mb-4 grid grid-cols-2 rounded-lg bg-zinc-200 p-1 text-sm">
        <button
          className={`rounded-md px-3 py-2 ${view === "workbench" && mode === "chat" ? "bg-white shadow-sm" : "text-zinc-600"}`}
          onClick={() => {
            setMode("chat");
            setView("workbench");
          }}
        >
          问答
        </button>
        <button
          className={`rounded-md px-3 py-2 ${view === "workbench" && mode === "workflow" ? "bg-white shadow-sm" : "text-zinc-600"}`}
          onClick={() => {
            setMode("workflow");
            setView("workbench");
          }}
        >
          拆解
        </button>
      </div>

      <button
        className={`mb-4 flex w-full items-center gap-2 rounded-lg border border-zinc-200 px-3 py-2 text-sm ${
          view === "settings"
            ? "bg-zinc-950 text-white"
            : "bg-white text-zinc-700 hover:bg-zinc-100"
        }`}
        onClick={() => setView("settings")}
      >
        <Settings size={15} />
        Settings
      </button>

      <section className="rounded-lg border border-zinc-200 bg-white p-3">
        <div className="mb-2 flex items-center justify-between">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Database size={15} />
            知识库
          </div>
          {busy ? <Loader2 size={14} className="animate-spin text-zinc-400" /> : null}
        </div>
        <p className="truncate text-xs text-zinc-500">
          {kbStats?.kb_path || "~/.scv/analysis"}
        </p>
        <p className="mt-1 text-xs text-zinc-500">
          {kbStats?.documents ?? 0} documents / {kbStats?.chunks ?? 0} chunks
        </p>
      </section>

      <section className="mt-4 rounded-lg border border-zinc-200 bg-white p-3">
        <h2 className="mb-3 flex items-center gap-2 text-sm font-medium">
          <History size={15} />
          Session
        </h2>
        <label className="block text-xs text-zinc-500">Session ID</label>
        <input
          className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-1.5 text-sm outline-none focus:border-zinc-400"
          value={sessionId}
          onChange={(event) => setSessionId(event.target.value)}
        />
        <div className="mt-3 flex gap-2">
          <input
            className="min-w-0 flex-1 rounded-md border border-zinc-200 px-2 py-1.5 text-sm outline-none focus:border-zinc-400"
            placeholder="搜索记忆"
            value={memoryQuery}
            onChange={(event) => setMemoryQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") onRunMemorySearch();
            }}
          />
          <button
            className="rounded-md border border-zinc-200 px-2.5 text-zinc-600 hover:bg-zinc-100"
            onClick={onRunMemorySearch}
          >
            <Search size={14} />
          </button>
        </div>
        <p className="mt-2 text-xs text-zinc-500">
          {memoryItems.length} records
        </p>
      </section>

      <section className="mt-4 rounded-lg border border-zinc-200 bg-white p-3">
        <h2 className="mb-3 flex items-center gap-2 text-sm font-medium">
          <History size={15} />
          Chat Records
        </h2>
        <div className="max-h-72 space-y-2 overflow-y-auto">
          {memoryItems.map((item) => (
            <button
              className="block w-full rounded-md border border-zinc-200 p-2 text-left hover:bg-zinc-50"
              key={item.id}
              onClick={() => setView("workbench")}
            >
              <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                <span className="font-medium">{item.role}</span>
                <span className="truncate text-zinc-400">{item.created_at}</span>
              </div>
              <p className="line-clamp-2 text-xs leading-5 text-zinc-600">
                {item.content}
              </p>
            </button>
          ))}
          {memoryItems.length === 0 ? (
            <p className="text-sm text-zinc-400">暂无聊天记录。</p>
          ) : null}
        </div>
      </section>

      <section className="mt-4 rounded-lg border border-zinc-200 bg-white p-3">
        <h2 className="mb-3 flex items-center gap-2 text-sm font-medium">
          <Braces size={15} />
          Workflows
        </h2>
        <div className="max-h-72 space-y-2 overflow-y-auto">
          {workflows.map((wf) => {
            const active = wf.workflow_id === workflowId;
            const stepLabel = wf.current_step
              ? STEP_LABELS[wf.current_step] ?? wf.current_step
              : "—";
            return (
              <button
                className={`block w-full rounded-md border p-2 text-left ${
                  active
                    ? "border-zinc-950 bg-zinc-50"
                    : "border-zinc-200 hover:bg-zinc-50"
                }`}
                key={wf.workflow_id}
                onClick={() => onPickWorkflow(wf.workflow_id)}
              >
                <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                  <span className="truncate font-medium">
                    {wf.project_name || wf.workflow_id.slice(0, 8)}
                  </span>
                  <span className="truncate text-zinc-400">{stepLabel}</span>
                </div>
                <div className="flex items-center justify-between text-xs text-zinc-500">
                  <span>{wf.status}</span>
                  <span className="truncate">{wf.created_at}</span>
                </div>
              </button>
            );
          })}
          {workflows.length === 0 ? (
            <p className="text-sm text-zinc-400">暂无拆解记录。</p>
          ) : null}
        </div>
      </section>
    </aside>
  );
}

function ThreadView({ error }: { error: string }) {
  return (
    <ThreadPrimitive.Root className="flex min-h-0 flex-1 flex-col">
      <ThreadPrimitive.Viewport className="min-h-0 flex-1 overflow-y-auto px-4 py-5">
        <div className="mx-auto flex max-w-4xl flex-col gap-4">
          <ThreadPrimitive.Messages
            components={{
              UserMessage,
              AssistantMessage,
            }}
          />
        </div>
      </ThreadPrimitive.Viewport>
      <div className="border-t border-zinc-200 p-4">
        {error ? (
          <div className="mx-auto mb-3 max-w-4xl rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {error}
          </div>
        ) : null}
        <Composer />
      </div>
    </ThreadPrimitive.Root>
  );
}

function UserMessage() {
  return (
    <MessagePrimitive.Root className="flex justify-end">
      <div className="max-w-[78%] rounded-2xl bg-zinc-950 px-4 py-3 text-sm leading-6 text-white">
        <MessagePrimitive.Content components={{ Text: MarkdownText }} />
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  const status = useMessage((message) => message.status);
  return (
    <MessagePrimitive.Root className="flex justify-start">
      <div className="max-w-[82%] rounded-2xl border border-zinc-200 bg-zinc-50 px-4 py-3 text-sm leading-6 text-zinc-900">
        <div className="mb-2 flex items-center gap-2 text-xs font-medium text-zinc-500">
          <Bot size={14} />
          Porto
          {status?.type === "running" ? (
            <Loader2 size={13} className="animate-spin" />
          ) : null}
        </div>
        <MessagePrimitive.Content
          components={{
            Text: MarkdownText,
            Source: SourcePart,
            Reasoning: ReasoningPart,
            tools: { Fallback: ToolPart },
          }}
        />
      </div>
    </MessagePrimitive.Root>
  );
}

function MarkdownText({ text }: { text: string }) {
  return (
    <div className="prose prose-zinc max-w-none prose-pre:rounded-lg prose-pre:bg-zinc-950 prose-pre:text-zinc-50">
      <ReactMarkdown rehypePlugins={[rehypeHighlight]} remarkPlugins={[remarkGfm]}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

function SourcePart({
  title,
  filename,
}: {
  title?: string;
  filename?: string;
}) {
  return (
    <div className="mt-2 rounded-md border border-zinc-200 bg-white px-3 py-2 text-xs text-zinc-600">
      <span className="font-medium">Source</span>
      <span className="ml-2">{title || filename}</span>
    </div>
  );
}

function ReasoningPart({ text }: { text: string }) {
  return (
    <details className="my-2 rounded-md border border-zinc-200 bg-white">
      <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-zinc-600">
        Reasoning
      </summary>
      <div className="px-3 pb-3 text-xs text-zinc-600">{text}</div>
    </details>
  );
}

function ToolPart({ toolName, args, result }: Record<string, unknown>) {
  return (
    <details className="my-2 rounded-md border border-zinc-200 bg-white">
      <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-zinc-600">
        Tool: {String(toolName)}
      </summary>
      <pre className="overflow-auto px-3 pb-3 text-xs">
        {JSON.stringify({ args, result }, null, 2)}
      </pre>
    </details>
  );
}

function Composer() {
  return (
    <ComposerPrimitive.Root className="mx-auto flex max-w-4xl items-end gap-2">
      <ComposerPrimitive.Input
        className="min-h-12 flex-1 resize-none rounded-xl border border-zinc-200 bg-white px-3 py-3 text-sm outline-none focus:border-zinc-400"
        placeholder="询问 ~/.scv/analysis 知识库..."
        rows={1}
      />
      <ComposerPrimitive.Send className="flex size-12 items-center justify-center rounded-xl bg-zinc-950 text-white transition hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40">
        <Send size={17} />
      </ComposerPrimitive.Send>
    </ComposerPrimitive.Root>
  );
}

function SettingsPage({
  agentConfig,
  busy,
  error,
  health,
  kbStats,
  ragConfig,
  onRefreshIndex,
  onSaveAgent,
  onSaveRag,
}: {
  agentConfig: AgentConfig;
  busy: boolean;
  error: string;
  health: HealthSnapshot | null;
  kbStats: KbStats | null;
  ragConfig: RagConfig;
  onRefreshIndex: (config?: RagConfig) => Promise<void>;
  onSaveAgent: (config: AgentConfig) => Promise<void>;
  onSaveRag: (config: RagConfig) => Promise<RagConfig | null>;
}) {
  const [section, setSection] = useState<SettingsSection>("rag");
  const [savedLabel, setSavedLabel] = useState("");

  const ri = kbStats?.rag_index;
  const progressPct =
    ri && ri.progress_total > 0 ? Math.round((ri.progress_done / ri.progress_total) * 100) : 0;
  const lastIndexed = ri?.last_indexed_at ? new Date(ri.last_indexed_at).toLocaleString() : null;
  const depLabels: Record<string, string> = {
    embedding: "Embedding",
    agent_llm: "Agent LLM",
    critic_llm: "Critic LLM",
  };
  const featLabels: Record<string, string> = {
    chat: "聊天",
    rag_search: "RAG 检索",
    workflow: "PRD 拆解",
  };
  const statusLabels: Record<string, string> = {
    idle: "空闲",
    running: "索引中",
    succeeded: "已完成",
    failed: "失败",
    interrupted: "已中断",
  };
  const depStatusColor: Record<string, string> = {
    ok: "text-emerald-600",
    degraded: "text-amber-600",
    down: "text-rose-600",
    unknown: "text-zinc-400",
  };

  const markSaved = (label: string) => {
    setSavedLabel(label);
    window.setTimeout(() => setSavedLabel(""), 1800);
  };

  async function saveKnowledge() {
    await onRefreshIndex();
    markSaved("Knowledge base indexed");
  }

  return (
    <div className="grid min-h-0 flex-1 grid-cols-1 bg-white md:grid-cols-[220px_minmax(0,1fr)]">
      <aside className="border-b border-zinc-200 bg-zinc-50 p-3 md:border-b-0 md:border-r">
        <div className="space-y-1">
          {[
            { id: "rag" as const, label: "RAG", icon: <Gauge size={15} /> },
            { id: "agent" as const, label: "Agent", icon: <Bot size={15} /> },
            {
              id: "knowledge" as const,
              label: "Knowledge",
              icon: <Database size={15} />,
            },
          ].map((item) => (
            <button
              className={`flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm ${
                section === item.id
                  ? "bg-zinc-950 text-white"
                  : "text-zinc-600 hover:bg-zinc-100"
              }`}
              key={item.id}
              onClick={() => setSection(item.id)}
            >
              {item.icon}
              {item.label}
            </button>
          ))}
        </div>
      </aside>

      <section className="min-w-0 overflow-y-auto p-5">
        {error ? (
          <div className="mb-4 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {error}
          </div>
        ) : null}
        {savedLabel ? (
          <div className="mb-4 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
            {savedLabel}
          </div>
        ) : null}

        {section === "rag" ? (
          <RagSettingsForm
            key={JSON.stringify(ragConfig)}
            busy={busy}
            ragConfig={ragConfig}
            onSaved={() => markSaved("RAG settings saved and indexed")}
            onRefreshIndex={onRefreshIndex}
            onSaveRag={onSaveRag}
          />
        ) : null}

        {section === "agent" ? (
          <AgentSettingsForm
            key={JSON.stringify(agentConfig)}
            agentConfig={agentConfig}
            busy={busy}
            onSaved={() => markSaved("Agent settings saved")}
            onSaveAgent={onSaveAgent}
          />
        ) : null}

        {section === "knowledge" ? (
          <SettingsCard
            busy={busy}
            saveLabel="Re-index"
            title="Knowledge Base"
            onSave={saveKnowledge}
          >
            <div className="grid gap-3 md:grid-cols-3">
              <div className="rounded-lg border border-zinc-200 bg-zinc-50 p-3">
                <p className="text-xs text-zinc-500">Path</p>
                <p className="mt-1 truncate text-sm font-medium">
                  {kbStats?.kb_path || "~/.scv/analysis"}
                </p>
              </div>
              <div className="rounded-lg border border-zinc-200 bg-zinc-50 p-3">
                <p className="text-xs text-zinc-500">Documents</p>
                <p className="mt-1 text-2xl font-semibold">
                  {kbStats?.documents ?? 0}
                </p>
              </div>
              <div className="rounded-lg border border-zinc-200 bg-zinc-50 p-3">
                <p className="text-xs text-zinc-500">Chunks</p>
                <p className="mt-1 text-2xl font-semibold">
                  {kbStats?.chunks ?? 0}
                </p>
              </div>
            </div>

            <div className="mt-4 rounded-lg border border-zinc-200 bg-zinc-50 p-3">
              <p className="text-xs text-zinc-500">知识库目录</p>
              <ul className="mt-2 space-y-1 text-sm">
                {ragConfig.kb_dirs.map((d, i) => (
                  <li key={`${d}-${i}`} className="flex items-center justify-between gap-2">
                    <span className="truncate text-zinc-700">{d}</span>
                    <button
                      className="text-xs text-rose-500 hover:underline"
                      type="button"
                      onClick={() =>
                        onSaveRag({
                          ...ragConfig,
                          kb_dirs: ragConfig.kb_dirs.filter((_, j) => j !== i),
                        })
                      }
                    >
                      删除
                    </button>
                  </li>
                ))}
              </ul>
              <form
                className="mt-2 flex gap-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  const input = event.currentTarget.elements.namedItem("dir") as HTMLInputElement | null;
                  const value = input?.value.trim();
                  if (value && !ragConfig.kb_dirs.includes(value)) {
                    void onSaveRag({ ...ragConfig, kb_dirs: [...ragConfig.kb_dirs, value] });
                    if (input) input.value = "";
                  }
                }}
              >
                <input
                  className="flex-1 rounded-md border border-zinc-200 px-2 py-1 text-sm"
                  name="dir"
                  placeholder="添加目录路径，如 ~/Documents/repo"
                />
                <button
                  className="rounded-md bg-zinc-950 px-3 py-1 text-sm text-white"
                  type="submit"
                >
                  添加
                </button>
              </form>
              <p className="mt-2 text-xs text-zinc-400">添加/删除目录后需点 Re-index 生效</p>
            </div>

            {ri ? (
              <div className="mt-4 rounded-lg border border-zinc-200 bg-zinc-50 p-3 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-zinc-700">
                    {ri.status === "running"
                      ? `索引中… ${ri.progress_done}/${ri.progress_total} 文档（${ri.chunks_done} chunks）`
                      : `索引状态：${statusLabels[ri.status] ?? ri.status}`}
                  </span>
                  {lastIndexed ? (
                    <span className="text-xs text-zinc-500">上次索引：{lastIndexed}</span>
                  ) : null}
                </div>
                {ri.status === "running" ? (
                  <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-zinc-200">
                    <div
                      className="h-full bg-zinc-950 transition-all"
                      style={{ width: `${progressPct}%` }}
                    />
                  </div>
                ) : null}
                {ri.error ? (
                  <p className="mt-2 text-xs text-rose-600">{ri.error}</p>
                ) : null}
              </div>
            ) : null}

            {health ? (
              <div className="mt-4 grid gap-3 md:grid-cols-2">
                <div className="rounded-lg border border-zinc-200 bg-zinc-50 p-3">
                  <p className="text-xs text-zinc-500">依赖健康度</p>
                  <ul className="mt-2 space-y-1 text-sm">
                    {health.dependencies.map((d) => (
                      <li key={d.name} className="flex items-center justify-between">
                        <span className="text-zinc-700">{depLabels[d.name] ?? d.name}</span>
                        <span className={depStatusColor[d.status] ?? "text-zinc-400"}>
                          {d.status}
                          {d.latency_ms != null ? ` · ${d.latency_ms}ms` : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
                <div className="rounded-lg border border-zinc-200 bg-zinc-50 p-3">
                  <p className="text-xs text-zinc-500">功能可用度</p>
                  <ul className="mt-2 space-y-1 text-sm">
                    {health.features.map((f) => (
                      <li key={f.name} className="flex items-center justify-between">
                        <span className="text-zinc-700">{featLabels[f.name] ?? f.name}</span>
                        <span className={f.available ? "text-emerald-600" : "text-rose-600"}>
                          {f.available ? "可用" : `不可用${f.reason ? `（${f.reason}）` : ""}`}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            ) : null}
          </SettingsCard>
        ) : null}
      </section>
    </div>
  );
}

function RagSettingsForm({
  busy,
  ragConfig,
  onSaved,
  onRefreshIndex,
  onSaveRag,
}: {
  busy: boolean;
  ragConfig: RagConfig;
  onSaved: () => void;
  onRefreshIndex: (config?: RagConfig) => Promise<void>;
  onSaveRag: (config: RagConfig) => Promise<RagConfig | null>;
}) {
  const [ragDraft, setRagDraft] = useState<RagConfig>(ragConfig);

  const updateRag = <K extends keyof RagConfig>(key: K, value: RagConfig[K]) => {
    setRagDraft((current) => ({ ...current, [key]: value }));
  };

  async function saveRag() {
    const saved = await onSaveRag(ragDraft);
    if (saved) onSaved();
  }

  return (
    <SettingsCard
      busy={busy}
      saveLabel="保存配置"
      title="RAG Settings"
      onSave={saveRag}
    >
      <div className="grid gap-4 md:grid-cols-2">
        <label className="block">
          <span className="text-xs text-zinc-500">Embedding Provider</span>
          <select
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            value={ragDraft.embedding_provider}
            onChange={(event) =>
              updateRag(
                "embedding_provider",
                event.target.value as RagConfig["embedding_provider"],
              )
            }
          >
            <option value="ollama">ollama</option>
            <option value="local">local</option>
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-zinc-500">Embedding Model</span>
          <input
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            value={ragDraft.embedding_model}
            onChange={(event) =>
              updateRag("embedding_model", event.target.value)
            }
          />
        </label>
        <label className="block md:col-span-2">
          <span className="text-xs text-zinc-500">Embedding Base URL</span>
          <input
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            value={ragDraft.embedding_base_url}
            onChange={(event) =>
              updateRag("embedding_base_url", event.target.value)
            }
          />
        </label>
        <label className="block">
          <span className="text-xs text-zinc-500">Chunk Size</span>
          <input
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            min={200}
            type="number"
            value={ragDraft.chunk_size}
            onChange={(event) =>
              updateRag("chunk_size", Number(event.target.value))
            }
          />
        </label>
        <label className="block">
          <span className="text-xs text-zinc-500">Chunk Overlap</span>
          <input
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            min={0}
            type="number"
            value={ragDraft.chunk_overlap}
            onChange={(event) =>
              updateRag("chunk_overlap", Number(event.target.value))
            }
          />
        </label>
        <label className="block md:col-span-2">
          <span className="text-xs text-zinc-500">Top K: {ragDraft.top_k}</span>
          <input
            className="mt-2 w-full accent-zinc-950"
            max={20}
            min={1}
            type="range"
            value={ragDraft.top_k}
            onChange={(event) => updateRag("top_k", Number(event.target.value))}
          />
        </label>
        <label className="block">
          <span className="text-xs text-zinc-500">检索算法</span>
          <select
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            value={ragDraft.retrieval_method}
            onChange={(event) =>
              updateRag("retrieval_method", event.target.value as RagConfig["retrieval_method"])
            }
          >
            <option value="hybrid">hybrid（向量 + BM25）</option>
            <option value="vector">vector（仅向量）</option>
            <option value="bm25">bm25（仅关键词）</option>
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-zinc-500">BM25 候选数（hybrid 用）</span>
          <input
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            type="number"
            min={1}
            value={ragDraft.bm25_top_k}
            onChange={(event) => updateRag("bm25_top_k", Number(event.target.value))}
          />
        </label>
        <label className="block md:col-span-2">
          <span className="text-xs text-zinc-500">
            向量 / BM25 融合权重（RRF，hybrid 用）：向量 {ragDraft.hybrid_vector_weight.toFixed(2)} ·
            BM25 {(1 - ragDraft.hybrid_vector_weight).toFixed(2)}
          </span>
          <input
            className="mt-2 w-full accent-zinc-950"
            max={1}
            min={0}
            step={0.05}
            type="range"
            value={ragDraft.hybrid_vector_weight}
            onChange={(event) =>
              updateRag("hybrid_vector_weight", Number(event.target.value))
            }
          />
        </label>
      </div>

      <div className="mt-6 border-t border-zinc-200 pt-4">
        <label className="flex items-center gap-2">
          <input
            checked={ragDraft.rerank_enabled}
            type="checkbox"
            onChange={(event) => updateRag("rerank_enabled", event.target.checked)}
          />
          <span className="text-sm font-medium text-zinc-700">
            启用重排序（LlamaIndex LLMRerank，检索候选后二次精排）
          </span>
        </label>
        <p className="mt-1 text-xs text-zinc-400">
          缺省复用 Agent 设置里的 Provider / Model / API Key；下方可单独覆盖。未配置可用 LLM 时自动降级为不重排。
        </p>
        <div className="mt-3 grid gap-4 md:grid-cols-2">
          <label className="block">
            <span className="text-xs text-zinc-500">重排序保留数量（Top N）</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="number"
              min={1}
              disabled={!ragDraft.rerank_enabled}
              value={ragDraft.rerank_top_n}
              onChange={(event) => updateRag("rerank_top_n", Number(event.target.value))}
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">重排序批大小（choice_batch_size）</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="number"
              min={1}
              disabled={!ragDraft.rerank_enabled}
              value={ragDraft.rerank_choice_batch_size}
              onChange={(event) =>
                updateRag("rerank_choice_batch_size", Number(event.target.value))
              }
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">重排序 Provider（留空复用 Agent）</span>
            <select
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              disabled={!ragDraft.rerank_enabled}
              value={ragDraft.rerank_provider ?? ""}
              onChange={(event) =>
                updateRag(
                  "rerank_provider",
                  (event.target.value || null) as RagConfig["rerank_provider"],
                )
              }
            >
              <option value="">（复用 Agent 设置）</option>
              <option value="openai">openai</option>
              <option value="anthropic">anthropic</option>
            </select>
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">重排序 Model（留空复用 Agent）</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              disabled={!ragDraft.rerank_enabled}
              placeholder="可选"
              value={ragDraft.rerank_model ?? ""}
              onChange={(event) => updateRag("rerank_model", event.target.value || null)}
            />
          </label>
        </div>
      </div>

      <div className="mt-4 flex items-center gap-3">
        <button
          className="rounded-md border border-zinc-300 px-3 py-2 text-sm text-zinc-700 hover:bg-zinc-100 disabled:opacity-40"
          type="button"
          disabled={busy}
          onClick={() => onRefreshIndex(ragDraft)}
        >
          Re-index
        </button>
        <span className="text-xs text-zinc-400">改动目录或切分参数后需手动 Re-index 生效</span>
      </div>
    </SettingsCard>
  );
}

function AgentSettingsForm({
  agentConfig,
  busy,
  onSaved,
  onSaveAgent,
}: {
  agentConfig: AgentConfig;
  busy: boolean;
  onSaved: () => void;
  onSaveAgent: (config: AgentConfig) => Promise<void>;
}) {
  const [agentDraft, setAgentDraft] = useState<AgentConfig>(agentConfig);

  const updateAgent = <K extends keyof AgentConfig>(
    key: K,
    value: AgentConfig[K],
  ) => {
    setAgentDraft((current) => ({ ...current, [key]: value }));
  };

  async function saveAgent() {
    await onSaveAgent(agentDraft);
    onSaved();
  }

  return (
    <SettingsCard busy={busy} title="Agent Settings" onSave={saveAgent}>
      <div className="grid gap-4 md:grid-cols-2">
        <label className="block">
          <span className="text-xs text-zinc-500">Provider</span>
          <select
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            value={agentDraft.agent_provider}
            onChange={(event) =>
              updateAgent(
                "agent_provider",
                event.target.value as AgentConfig["agent_provider"],
              )
            }
          >
            <option value="openai">openai</option>
            <option value="anthropic">anthropic</option>
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-zinc-500">Model</span>
          <input
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            value={agentDraft.agent_model}
            onChange={(event) =>
              updateAgent("agent_model", event.target.value)
            }
          />
        </label>
        <label className="block md:col-span-2">
          <span className="text-xs text-zinc-500">Base URL</span>
          <input
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            placeholder="可选"
            value={agentDraft.agent_base_url ?? ""}
            onChange={(event) =>
              updateAgent("agent_base_url", event.target.value)
            }
          />
        </label>
        <label className="block md:col-span-2">
          <span className="text-xs text-zinc-500">API Key</span>
          <input
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            placeholder="留空则禁用 LLM"
            type="password"
            value={agentDraft.agent_api_key ?? ""}
            onChange={(event) =>
              updateAgent("agent_api_key", event.target.value)
            }
          />
        </label>
        <label className="block">
          <span className="text-xs text-zinc-500">
            Temperature: {agentDraft.agent_temperature}
          </span>
          <input
            className="mt-2 w-full accent-zinc-950"
            max={2}
            min={0}
            step={0.1}
            type="range"
            value={agentDraft.agent_temperature}
            onChange={(event) =>
              updateAgent("agent_temperature", Number(event.target.value))
            }
          />
        </label>
        <label className="block">
          <span className="text-xs text-zinc-500">Max Tokens</span>
          <input
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            min={1}
            type="number"
            value={agentDraft.agent_max_tokens}
            onChange={(event) =>
              updateAgent("agent_max_tokens", Number(event.target.value))
            }
          />
        </label>
      </div>

      <div className="mt-5 rounded-lg border border-zinc-200 p-4">
        <p className="text-sm font-semibold text-zinc-700">Critic LLM</p>
        <p className="text-xs text-zinc-400">独立评审模型，留空复用 generator</p>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block">
            <span className="text-xs text-zinc-500">Provider</span>
            <select
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              value={agentDraft.critic_provider ?? ""}
              onChange={(event) =>
                updateAgent(
                  "critic_provider",
                  (event.target.value || null) as AgentConfig["critic_provider"],
                )
              }
            >
              <option value="">（复用 generator）</option>
              <option value="openai">openai</option>
              <option value="anthropic">anthropic</option>
            </select>
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">Model</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              value={agentDraft.critic_model ?? ""}
              onChange={(event) => updateAgent("critic_model", event.target.value || null)}
            />
          </label>
          <label className="block md:col-span-2">
            <span className="text-xs text-zinc-500">Base URL</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              value={agentDraft.critic_base_url ?? ""}
              onChange={(event) => updateAgent("critic_base_url", event.target.value || null)}
            />
          </label>
          <label className="block md:col-span-2">
            <span className="text-xs text-zinc-500">API Key</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="password"
              value={agentDraft.critic_api_key ?? ""}
              onChange={(event) => updateAgent("critic_api_key", event.target.value || null)}
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">Temperature</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="number"
              step="0.1"
              min="0"
              max="2"
              value={agentDraft.critic_temperature ?? 0.1}
              onChange={(event) => updateAgent("critic_temperature", Number(event.target.value))}
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">Max Tokens</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="number"
              min="1"
              value={agentDraft.critic_max_tokens ?? 1500}
              onChange={(event) => updateAgent("critic_max_tokens", Number(event.target.value))}
            />
          </label>
        </div>
      </div>

      <details className="mt-4">
        <summary className="cursor-pointer text-xs text-zinc-500">
          高级配置（Spec loop · Workflow · Memory · Context）
        </summary>
        <div className="mt-3 grid gap-4 md:grid-cols-2">
          <label className="block">
            <span className="text-xs text-zinc-500">Spec refine 启用</span>
            <input
              className="mt-2 h-4 w-4 accent-zinc-950"
              type="checkbox"
              checked={agentDraft.spec_refine_enabled}
              onChange={(event) =>
                updateAgent("spec_refine_enabled", event.target.checked)
              }
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">Spec refine 最大迭代</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="number"
              min={0}
              max={10}
              value={agentDraft.spec_refine_max_iter}
              onChange={(event) =>
                updateAgent("spec_refine_max_iter", Number(event.target.value))
              }
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">Spec 并发度（1-10）</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="number"
              min={1}
              max={10}
              value={agentDraft.spec_refine_concurrency}
              onChange={(event) =>
                updateAgent("spec_refine_concurrency", Number(event.target.value))
              }
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">Spec PASS 分数（满分 12）</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="number"
              min={0}
              max={12}
              value={agentDraft.spec_refine_pass_score}
              onChange={(event) =>
                updateAgent("spec_refine_pass_score", Number(event.target.value))
              }
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">Workflow 回边启用</span>
            <input
              className="mt-2 h-4 w-4 accent-zinc-950"
              type="checkbox"
              checked={agentDraft.workflow_rework_enabled}
              onChange={(event) =>
                updateAgent("workflow_rework_enabled", event.target.checked)
              }
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">Workflow 回边上限</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="number"
              min={0}
              max={5}
              value={agentDraft.workflow_rework_max_passes}
              onChange={(event) =>
                updateAgent(
                  "workflow_rework_max_passes",
                  Number(event.target.value),
                )
              }
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">Memory 压缩阈值（消息数）</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="number"
              min={4}
              value={agentDraft.memory_compact_threshold}
              onChange={(event) =>
                updateAgent(
                  "memory_compact_threshold",
                  Number(event.target.value),
                )
              }
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">Memory 近期保留条数</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="number"
              min={1}
              value={agentDraft.memory_recent_keep}
              onChange={(event) =>
                updateAgent("memory_recent_keep", Number(event.target.value))
              }
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">Context 字符预算</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="number"
              min={1000}
              value={agentDraft.context_char_budget}
              onChange={(event) =>
                updateAgent("context_char_budget", Number(event.target.value))
              }
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">原生流式输出</span>
            <input
              className="mt-2 h-4 w-4 accent-zinc-950"
              type="checkbox"
              checked={agentDraft.agent_stream_enabled}
              onChange={(event) =>
                updateAgent("agent_stream_enabled", event.target.checked)
              }
            />
          </label>
          <label className="block">
            <span className="text-xs text-zinc-500">节点 tool 最大轮数</span>
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              type="number"
              min={1}
              max={20}
              value={agentDraft.agent_max_tool_turns}
              onChange={(event) =>
                updateAgent("agent_max_tool_turns", Number(event.target.value))
              }
            />
          </label>
        </div>
      </details>
    </SettingsCard>
  );
}

function SettingsCard({
  busy,
  children,
  onSave,
  saveLabel = "Save",
  title,
}: {
  busy: boolean;
  children: React.ReactNode;
  onSave: () => Promise<void>;
  saveLabel?: string;
  title: string;
}) {
  return (
    <div className="max-w-3xl">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 className="text-base font-semibold">{title}</h2>
        <button
          className="flex items-center gap-2 rounded-lg bg-zinc-950 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-800 disabled:opacity-40"
          disabled={busy}
          onClick={() => void onSave()}
        >
          {busy ? <Loader2 size={15} className="animate-spin" /> : null}
          {saveLabel}
        </button>
      </div>
      <div className="rounded-xl border border-zinc-200 bg-white p-4">
        {children}
      </div>
    </div>
  );
}

function WorkflowPanel({
  busy,
  detail,
  draft,
  error,
  fileInputRef,
  onAdvance,
  onRun,
  onSaveStep,
  onTextChange,
  projectName,
  selectedFile,
  setDraft,
  setProjectName,
  setSelectedFile,
  text,
}: {
  busy: boolean;
  detail: WorkflowDetail | null;
  draft: string;
  error: string;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  onAdvance: () => void;
  onRun: () => void;
  onSaveStep: (step: WorkflowStepName, output: Record<string, unknown>) => void;
  onTextChange: (value: string) => void;
  projectName: string;
  selectedFile: File | null;
  setDraft: (value: string) => void;
  setProjectName: (value: string) => void;
  setSelectedFile: (value: File | null) => void;
  text: string;
}) {
  const curStep = detail?.current_step ?? null;
  const curIdx = curStep ? WORKFLOW_STEPS.indexOf(curStep) : -1;
  const showCheckpoint =
    !!detail &&
    detail.status === "awaiting_input" &&
    !!curStep &&
    CHECKPOINT_STEPS.includes(curStep);

  return (
    <div className="flex flex-1 flex-col p-4">
      {error ? (
        <div className="mb-3 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </div>
      ) : null}

      {!detail ? (
        <>
          <div className="mb-3 grid gap-3 md:grid-cols-[minmax(0,1fr)_260px]">
            <label className="block">
              <span className="text-xs text-zinc-500">项目名称</span>
              <input
                className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm outline-none focus:border-zinc-400"
                placeholder="可选"
                value={projectName}
                onChange={(event) => setProjectName(event.target.value)}
              />
            </label>
            <label className="block">
              <span className="text-xs text-zinc-500">PRD 文件</span>
              <span className="mt-1 flex cursor-pointer items-center gap-2 rounded-md border border-zinc-200 px-3 py-2 text-sm hover:bg-zinc-50">
                <Upload size={15} />
                <span className="truncate">
                  {selectedFile?.name || "上传 PDF / Word / Markdown"}
                </span>
                <input
                  ref={fileInputRef}
                  className="hidden"
                  type="file"
                  accept=".pdf,.docx,.md,.txt"
                  onChange={(event) =>
                    setSelectedFile(event.target.files?.[0] ?? null)
                  }
                />
              </span>
            </label>
          </div>
          <textarea
            className="min-h-[300px] flex-1 resize-none rounded-xl border border-zinc-200 bg-zinc-50 p-4 text-sm leading-6 outline-none focus:border-zinc-400"
            placeholder="粘贴一句业务需求或完整 PRD 文本..."
            value={text}
            onChange={(event) => onTextChange(event.target.value)}
          />
          <p className="mt-2 truncate text-xs text-zinc-500">
            {selectedFile
              ? `将使用上传文件：${selectedFile.name}`
              : "未上传文件时，将使用文本框内容。"}
          </p>
        </>
      ) : null}

      {detail ? (
        <>
          <WorkflowStepper curIdx={curIdx} status={detail.status} />

          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
            <span className="rounded-md border border-zinc-200 bg-zinc-50 px-2 py-1">
              workflow: {detail.workflow_id.slice(0, 8)}
            </span>
            <span className="rounded-md border border-zinc-200 bg-zinc-50 px-2 py-1">
              status: {detail.status}
            </span>
            {detail.project_name ? (
              <span className="rounded-md border border-zinc-200 bg-zinc-50 px-2 py-1">
                {detail.project_name}
              </span>
            ) : null}
          </div>

          {detail.status === "running" ? (
            <div className="my-6 flex items-center gap-2 text-sm text-zinc-500">
              <Loader2 className="animate-spin" size={16} />
              生成中…
            </div>
          ) : null}

          {detail.status === "failed" ? (
            <div className="my-4 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
              {detail.error || "步骤失败"}
              <button
                className="ml-2 underline"
                onClick={onAdvance}
                type="button"
              >
                重试
              </button>
            </div>
          ) : null}

          {showCheckpoint && curStep ? (
            <CheckpointEditor
              detail={detail}
              draft={draft}
              onAdvance={onAdvance}
              onSaveStep={onSaveStep}
              setDraft={setDraft}
              step={curStep}
            />
          ) : null}

          {detail.status === "completed" ? (
            <CompletedView detail={detail} />
          ) : null}
        </>
      ) : null}

      <div className="mt-3 flex items-center justify-end gap-3">
        <button
          className="flex items-center gap-2 rounded-lg bg-zinc-950 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-800 disabled:opacity-40"
          disabled={busy || (!text.trim() && !selectedFile)}
          onClick={onRun}
          type="button"
        >
          {busy ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
          {detail ? "重新拆解" : "运行拆解"}
        </button>
      </div>
    </div>
  );
}

function WorkflowStepper({
  curIdx,
  status,
}: {
  curIdx: number;
  status: WorkflowDetail["status"];
}) {
  // completed 时所有 step 标记完成
  const effectiveIdx = status === "completed" ? WORKFLOW_STEPS.length : curIdx;
  return (
    <ol className="flex flex-wrap items-center gap-2 rounded-lg border border-zinc-200 bg-zinc-50 p-3">
      {WORKFLOW_STEPS.map((step, idx) => {
        const done = idx < effectiveIdx;
        const current = idx === effectiveIdx && status !== "completed";
        const label = STEP_LABELS[step];
        const dotClass = done
          ? "bg-emerald-500 text-white"
          : current
            ? "bg-zinc-950 text-white"
            : "bg-zinc-200 text-zinc-500";
        return (
          <li key={step} className="flex items-center gap-2">
            <span
              className={`flex size-6 items-center justify-center rounded-full text-xs font-medium ${dotClass}`}
            >
              {done ? <CheckCircle2 size={14} /> : idx + 1}
            </span>
            <span
              className={`text-sm ${current ? "font-semibold text-zinc-900" : done ? "text-zinc-700" : "text-zinc-400"}`}
            >
              {label}
            </span>
            {idx < WORKFLOW_STEPS.length - 1 ? (
              <span className="mx-1 text-zinc-300">›</span>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

function CheckpointEditor({
  detail,
  draft,
  onAdvance,
  onSaveStep,
  setDraft,
  step,
}: {
  detail: WorkflowDetail;
  draft: string;
  onAdvance: () => void;
  onSaveStep: (step: WorkflowStepName, output: Record<string, unknown>) => void;
  setDraft: (value: string) => void;
  step: WorkflowStepName;
}) {
  if (step === "understand") {
    return (
      <MarkdownCheckpoint
        key="understand"
        draft={draft}
        onAdvance={onAdvance}
        onSave={() => onSaveStep("understand", { understanding: draft })}
        setDraft={setDraft}
        title="理解（understanding）"
      />
    );
  }
  if (step === "generate") {
    return (
      <MarkdownCheckpoint
        key="generate"
        draft={draft}
        onAdvance={onAdvance}
        onSave={() => onSaveStep("generate", { specs: parseSpecsDraft(draft) })}
        setDraft={setDraft}
        title="规格（specs，使用 ## 子系统名 分段）"
      />
    );
  }
  if (step === "identify") {
    return (
      <SubsystemCheckpoint
        detail={detail}
        key={`${detail.workflow_id}:${detail.outputs.identify?.produced_at ?? ""}`}
        onAdvance={onAdvance}
        onSaveStep={onSaveStep}
      />
    );
  }
  return null;
}

function MarkdownCheckpoint({
  draft,
  onAdvance,
  onSave,
  setDraft,
  title,
}: {
  draft: string;
  onAdvance: () => void;
  onSave: () => void;
  setDraft: (value: string) => void;
  title: string;
}) {
  const [preview, setPreview] = useState(false);
  return (
    <div className="my-4 flex flex-1 flex-col rounded-xl border border-zinc-200 bg-white">
      <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        <div className="flex items-center gap-2 text-xs">
          <button
            className={`rounded-md px-2 py-1 ${!preview ? "bg-zinc-950 text-white" : "text-zinc-600 hover:bg-zinc-100"}`}
            onClick={() => setPreview(false)}
            type="button"
          >
            编辑
          </button>
          <button
            className={`rounded-md px-2 py-1 ${preview ? "bg-zinc-950 text-white" : "text-zinc-600 hover:bg-zinc-100"}`}
            onClick={() => setPreview(true)}
            type="button"
          >
            预览
          </button>
        </div>
      </div>
      {preview ? (
        <div className="prose prose-zinc max-w-none flex-1 overflow-y-auto p-4 prose-pre:rounded-lg prose-pre:bg-zinc-950 prose-pre:text-zinc-50">
          <ReactMarkdown rehypePlugins={[rehypeHighlight]} remarkPlugins={[remarkGfm]}>
            {draft || "_（空）_"}
          </ReactMarkdown>
        </div>
      ) : (
        <textarea
          className="min-h-[280px] flex-1 resize-none rounded-b-xl border-0 p-4 text-sm leading-6 outline-none focus:ring-0"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
      )}
      <div className="flex items-center justify-end gap-2 border-t border-zinc-200 px-4 py-2">
        <button
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm text-zinc-700 hover:bg-zinc-100"
          onClick={onSave}
          type="button"
        >
          保存修改
        </button>
        <button
          className="rounded-md bg-zinc-950 px-3 py-1.5 text-sm text-white hover:bg-zinc-800"
          onClick={onAdvance}
          type="button"
        >
          继续下一步
        </button>
      </div>
    </div>
  );
}

function SubsystemCheckpoint({
  detail,
  onAdvance,
  onSaveStep,
}: {
  detail: WorkflowDetail;
  onAdvance: () => void;
  onSaveStep: (step: WorkflowStepName, output: Record<string, unknown>) => void;
}) {
  const [items, setItems] = useState<Subsystem[]>(() => readSubsystems(detail));

  function update(idx: number, patch: Partial<Subsystem>) {
    setItems((current) =>
      current.map((item, i) => (i === idx ? { ...item, ...patch } : item)),
    );
  }
  function remove(idx: number) {
    setItems((current) => current.filter((_, i) => i !== idx));
  }
  function add() {
    setItems((current) => [
      ...current,
      { ...EMPTY_SUBSYSTEM, name: `Subsystem ${current.length + 1}` },
    ]);
  }
  function saveAll() {
    onSaveStep("identify", { subsystems: items });
  }

  return (
    <div className="my-4 flex flex-1 flex-col rounded-xl border border-zinc-200 bg-white">
      <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-2">
        <h3 className="text-sm font-semibold">子系统（subsystems）</h3>
        <button
          className="flex items-center gap-1 rounded-md border border-zinc-300 px-2 py-1 text-xs text-zinc-700 hover:bg-zinc-100"
          onClick={add}
          type="button"
        >
          <Plus size={13} />
          添加
        </button>
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {items.map((sub, idx) => (
          <div
            className="rounded-lg border border-zinc-200 bg-zinc-50 p-3"
            key={`${sub.name}-${idx}`}
          >
            <div className="mb-2 flex items-center gap-2">
              <input
                className="flex-1 rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-sm font-medium outline-none focus:border-zinc-400"
                placeholder="子系统名称"
                value={sub.name}
                onChange={(event) => update(idx, { name: event.target.value })}
              />
              <select
                className="rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-sm"
                value={sub.type}
                onChange={(event) =>
                  update(idx, {
                    type: event.target.value as Subsystem["type"],
                  })
                }
              >
                <option value="new">new</option>
                <option value="extend">extend</option>
                <option value="existing">existing</option>
              </select>
              <button
                className="rounded-md p-1.5 text-rose-500 hover:bg-rose-50"
                onClick={() => remove(idx)}
                title="删除子系统"
                type="button"
              >
                <Trash2 size={15} />
              </button>
            </div>
            <label className="block">
              <span className="text-xs text-zinc-500">职责（responsibility）</span>
              <textarea
                className="mt-1 min-h-[60px] w-full resize-y rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-sm outline-none focus:border-zinc-400"
                value={sub.responsibility}
                onChange={(event) =>
                  update(idx, { responsibility: event.target.value })
                }
              />
            </label>
            <div className="mt-2 grid gap-2 md:grid-cols-3">
              <label className="block">
                <span className="text-xs text-zinc-500">
                  capabilities（逗号分隔）
                </span>
                <input
                  className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-sm outline-none focus:border-zinc-400"
                  value={sub.capabilities.join(", ")}
                  onChange={(event) =>
                    update(idx, {
                      capabilities: splitCsv(event.target.value),
                    })
                  }
                />
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500">
                  data_entities（逗号分隔）
                </span>
                <input
                  className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-sm outline-none focus:border-zinc-400"
                  value={sub.data_entities.join(", ")}
                  onChange={(event) =>
                    update(idx, {
                      data_entities: splitCsv(event.target.value),
                    })
                  }
                />
              </label>
              <label className="block">
                <span className="text-xs text-zinc-500">
                  dependencies（逗号分隔）
                </span>
                <input
                  className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-sm outline-none focus:border-zinc-400"
                  value={sub.dependencies.join(", ")}
                  onChange={(event) =>
                    update(idx, {
                      dependencies: splitCsv(event.target.value),
                    })
                  }
                />
              </label>
            </div>
          </div>
        ))}
        {items.length === 0 ? (
          <p className="text-sm text-zinc-400">
            还没有任何子系统，点击右上角「添加」开始。
          </p>
        ) : null}
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-zinc-200 px-4 py-2">
        <button
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm text-zinc-700 hover:bg-zinc-100"
          onClick={saveAll}
          type="button"
        >
          保存修改
        </button>
        <button
          className="rounded-md bg-zinc-950 px-3 py-1.5 text-sm text-white hover:bg-zinc-800"
          onClick={onAdvance}
          type="button"
        >
          继续下一步
        </button>
      </div>
    </div>
  );
}

function CompletedView({ detail }: { detail: WorkflowDetail }) {
  const understanding = detail.outputs.understand?.output.understanding;
  const subsystems = readSubsystems(detail);
  const specs = detail.outputs.generate?.output.specs as
    | Record<string, string>
    | undefined;
  const evaluateOutput = detail.outputs.evaluate?.output;
  const score =
    evaluateOutput && typeof evaluateOutput === "object" && "score" in evaluateOutput
      ? Number((evaluateOutput as Record<string, unknown>).score)
      : null;
  const [tab, setTab] = useState<"understand" | "subsystems" | "specs">(
    "understand",
  );

  return (
    <div className="my-4 flex flex-1 flex-col rounded-xl border border-zinc-200 bg-white">
      <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <CheckCircle2 size={15} className="text-emerald-500" />
          拆解完成
        </h3>
        {score != null ? (
          <span className={`rounded-full px-2 py-1 text-xs ${scoreClass(score)}`}>
            score {score}
          </span>
        ) : null}
      </div>
      <div className="flex gap-1 border-b border-zinc-200 px-2 py-1">
        {(
          [
            ["understand", "理解"],
            ["subsystems", "子系统"],
            ["specs", "规格"],
          ] as const
        ).map(([key, label]) => (
          <button
            className={`rounded-md px-3 py-1.5 text-sm ${
              tab === key
                ? "bg-zinc-950 text-white"
                : "text-zinc-600 hover:bg-zinc-100"
            }`}
            key={key}
            onClick={() => setTab(key)}
            type="button"
          >
            {label}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-y-auto p-4">
        {tab === "understand" ? (
          <div className="prose prose-zinc max-w-none prose-pre:rounded-lg prose-pre:bg-zinc-950 prose-pre:text-zinc-50">
            <ReactMarkdown
              rehypePlugins={[rehypeHighlight]}
              remarkPlugins={[remarkGfm]}
            >
              {typeof understanding === "string"
                ? understanding
                : "_（无 understanding 输出）_"}
            </ReactMarkdown>
          </div>
        ) : null}
        {tab === "subsystems" ? (
          <div className="grid gap-2 md:grid-cols-2">
            {subsystems.map((sub, idx) => (
              <div
                className="rounded-lg border border-zinc-200 bg-zinc-50 p-3"
                key={`${sub.name}-${idx}`}
              >
                <div className="mb-1 flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium">{sub.name}</span>
                  <span
                    className={`rounded-full px-2 py-1 text-xs ${subsystemClass(sub)}`}
                  >
                    {sub.type}
                  </span>
                </div>
                <p className="line-clamp-3 text-xs leading-5 text-zinc-600">
                  {sub.responsibility}
                </p>
              </div>
            ))}
            {subsystems.length === 0 ? (
              <p className="text-sm text-zinc-400">无子系统输出。</p>
            ) : null}
          </div>
        ) : null}
        {tab === "specs" ? (
          <div className="space-y-3">
            {specs
              ? Object.entries(specs).map(([name, body]) => (
                  <details
                    className="rounded-lg border border-zinc-200 bg-zinc-50"
                    key={name}
                  >
                    <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
                      {name}
                    </summary>
                    <div className="prose prose-zinc max-w-none px-3 pb-3 prose-pre:rounded-lg prose-pre:bg-zinc-950 prose-pre:text-zinc-50">
                      <ReactMarkdown
                        rehypePlugins={[rehypeHighlight]}
                        remarkPlugins={[remarkGfm]}
                      >
                        {body}
                      </ReactMarkdown>
                    </div>
                  </details>
                ))
              : null}
            {!specs || Object.keys(specs).length === 0 ? (
              <p className="text-sm text-zinc-400">无规格输出。</p>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function Inspector({
  inspector,
  memoryItems,
}: {
  inspector: InspectorState;
  memoryItems: MemoryRecord[];
}) {
  return (
    <aside className="min-w-0 overflow-y-auto border-t border-zinc-200 bg-zinc-50 p-4 lg:border-t-0">
      <InspectorSection icon={<Play size={15} />} title="Agent Steps">
        <div className="space-y-2">
          {inspector.steps.map((step) => (
            <details
              className="rounded-lg border border-zinc-200 bg-white p-3"
              key={step.name}
            >
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2">
                <span className="truncate text-sm font-medium">{step.name}</span>
                <span className="flex items-center gap-1 text-emerald-600">
                  <CheckCircle2 size={15} />
                  <ChevronDown size={13} />
                </span>
              </summary>
              <p className="mt-2 text-xs leading-5 text-zinc-600">
                {step.summary}
              </p>
            </details>
          ))}
          {inspector.steps.length === 0 ? <EmptyText text="尚无执行步骤。" /> : null}
        </div>
      </InspectorSection>

      {inspector.evaluation ? <EvalCard evaluation={inspector.evaluation} /> : null}

      <InspectorSection icon={<History size={15} />} title="Memory Search">
        <ChunkList chunks={inspector.memory} empty="暂无记忆命中。" />
      </InspectorSection>

      <InspectorSection icon={<History size={15} />} title="Chat Records">
        <div className="max-h-80 space-y-2 overflow-y-auto">
          {memoryItems.map((item) => (
            <div className="rounded-lg border border-zinc-200 bg-white p-2" key={item.id}>
              <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                <span className="font-medium">{item.role}</span>
                <span className="truncate text-zinc-400">{item.created_at}</span>
              </div>
              <p className="line-clamp-3 text-xs leading-5 text-zinc-600">
                {item.content}
              </p>
            </div>
          ))}
          {memoryItems.length === 0 ? <EmptyText text="暂无聊天记录。" /> : null}
        </div>
      </InspectorSection>

      <InspectorSection icon={<FileInput size={15} />} title="Sources">
        <ChunkList chunks={inspector.sources} empty="暂无引用片段。" />
      </InspectorSection>
    </aside>
  );
}

function InspectorSection({
  children,
  icon,
  title,
}: {
  children: React.ReactNode;
  icon: React.ReactNode;
  title: string;
}) {
  return (
    <section className="mb-6">
      <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
        {icon}
        {title}
      </h2>
      {children}
    </section>
  );
}

function EvalCard({ evaluation }: { evaluation: ChatResponseEval }) {
  return (
    <InspectorSection icon={<Gauge size={15} />} title="RAG Eval">
      <div className="rounded-lg border border-zinc-200 bg-white p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-sm font-medium">Score</span>
          <span className={`rounded-full px-2 py-1 text-xs ${scoreClass(evaluation.score)}`}>
            {evaluation.score}
          </span>
        </div>
        {evaluation.cases[0] ? (
          <div className="space-y-1 text-xs text-zinc-600">
            {Object.entries(evaluation.cases[0].metrics).map(([key, value]) => (
              <div className="flex justify-between gap-3" key={key}>
                <span className="truncate">{key}</span>
                <span>{value}</span>
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </InspectorSection>
  );
}

function ChunkList({
  chunks,
  empty,
}: {
  chunks: SourceChunk[];
  empty: string;
}) {
  return (
    <div className="space-y-2">
      {chunks.map((chunk) => (
        <details className="rounded-lg border border-zinc-200 bg-white" key={chunk.id}>
          <summary className="cursor-pointer px-3 py-2 text-sm">
            <span className="block truncate font-medium text-zinc-700">{chunk.path || chunk.title}</span>
            <span className="text-xs text-zinc-400">{chunk.title} · score {chunk.score}</span>
          </summary>
          <div className="px-3 pb-3 text-xs leading-5 text-zinc-600">
            {chunk.text}
          </div>
        </details>
      ))}
      {chunks.length === 0 ? <EmptyText text={empty} /> : null}
    </div>
  );
}

function EmptyText({ text }: { text: string }) {
  return <p className="text-sm text-zinc-400">{text}</p>;
}
