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
  Download,
  FileInput,
  Gauge,
  History,
  Loader2,
  Network,
  Pencil,
  Play,
  Plus,
  Search,
  Send,
  Settings,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MarkdownView } from "./markdown-view";
import {
  advanceWorkflow,
  createWorkflow,
  createWorkflowUpload,
  defaultAgentConfig,
  defaultDocumentConfig,
  defaultRagConfig,
  getAppSettings,
  getHealth,
  getKbStats,
  getWorkflow,
  indexKnowledgeBase,
  listMemory,
  rerunStep,
  saveAppSettings,
  saveStepOutput,
  updateWorkflowSpec,
  uploadChatFile,
} from "@/lib/api";
import type {
  AgentConfig,
  ChatResponseEval,
  DocumentConfig,
  HealthSnapshot,
  InspectorState,
  KbStats,
  MemoryRecord,
  RagConfig,
  SourceChunk,
  Subsystem,
  ToolMeta,
  WorkflowDetail,
  WorkflowOutputEntry,
  WorkflowStepName,
} from "@/lib/types";
import { ArchitectureView } from "@/components/architecture-view";
import { SessionList } from "@/components/session-list";
import { WorkflowList } from "@/components/workflow-list";
import dynamic from "next/dynamic";
import { sanitizeFilename, triggerDownload } from "@/lib/download";

const SpecMdxEditor = dynamic(
  () => import("./spec-mdx-editor").then((m) => m.SpecMdxEditor),
  { ssr: false, loading: () => <Loader2 size={16} className="animate-spin" /> },
);

type Mode = "chat" | "workflow";
type View = "workbench" | "settings";
type SettingsSection = "rag" | "agent" | "document" | "knowledge" | "architecture";

type ChatAttachmentStatus = "uploading" | "done" | "error";
type ChatAttachment = {
  id: string;
  file: File;
  status: ChatAttachmentStatus;
  fileId?: string;
  pageCount?: number;
  error?: string;
};

const emptyInspector: InspectorState = {
  steps: [],
  sources: [],
  memory: [],
  evaluation: null,
};

const WELCOME_TEXT =
  "选择知识库问答或 Porto PRD 拆解模式。问答会检索知识库和会话记忆；拆解模式会输出子系统、规格和评估结果。";

const WORKFLOW_STEPS: WorkflowStepName[] = [
  "retrieve",
  "understand",
  "identify",
  "generate",
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

/**
 * 解析 generate 步骤的 markdown 草稿（## name 切分）回 specs dict。
 * readStepDraft 用 "\n\n---\n\n" 连接多段 specs，split 后第二段起会带前导空白，
 * 必须 trim 每个 block 再匹配，否则 ^## 锚定失败 → 多 spec 静默丢失。
 * body 设为可选以兼容仅有标题的 spec。
 */
function parseSpecsDraft(draft: string): Record<string, string> {
  const result: Record<string, string> = {};
  const blocks = draft.split(/^---$/m);
  for (const block of blocks) {
    const match = block.trim().match(/^##\s+(.+?)(?:\s*\n([\s\S]*))?$/);
    if (match) {
      const name = match[1].trim();
      const body = (match[2] ?? "").trim();
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

/**
 * 工具循环硬上限(与后端 settings.tool_turn_hard_cap 对齐)。
 * rerun 按钮的"撞顶"判定与前端预算显示 newMax 都以此为天花板。
 */
const TOOL_TURN_HARD_CAP = 40;

/**
 * 后端把单步 tool_meta 写在 entry.output.tool_meta(见 workflow_executor._tool_meta_for:
 * 单步从 AgentStep.data 取、generate 聚合 any truncated)。types.ts 的 WorkflowOutputEntry
 * 顶层也声明了 tool_meta?,但实际 wire 上不存在 —— 统一从 output 取,避免静默 undefined。
 */
function readStepToolMeta(
  entry: WorkflowOutputEntry | undefined,
): ToolMeta | undefined {
  if (!entry) return undefined;
  const tm = entry.output.tool_meta;
  if (tm && typeof tm === "object") return tm as ToolMeta;
  return undefined;
}

/**
 * generate 步的 per-subsystem tool_meta:output.spec_results[name].tool_meta。
 * 后端 _to_jsonable(SpecResult) → model_dump,含 tool_meta: dict。
 * 截断 UI(卡片左色条 + 子系统超限 chip)按此判定。
 */
type SpecResultWire = {
  final?: string;
  truncated?: boolean;
  used_llm?: boolean;
  tool_meta?: ToolMeta;
};

function readSpecResults(
  detail: WorkflowDetail,
): Record<string, SpecResultWire> {
  const raw = detail.outputs.generate?.output.spec_results;
  if (!raw || typeof raw !== "object") return {};
  return raw as Record<string, SpecResultWire>;
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
  const [documentConfig, setDocumentConfig] =
    useState<DocumentConfig>(defaultDocumentConfig);
  const [kbStats, setKbStats] = useState<KbStats | null>(null);
  const [health, setHealth] = useState<HealthSnapshot | null>(null);
  const [backendOnline, setBackendOnline] = useState(true);
  const [memoryItems, setMemoryItems] = useState<MemoryRecord[]>([]);
  const [workflowRefreshKey, setWorkflowRefreshKey] = useState(0);
  const [inspector, setInspector] = useState<InspectorState>(emptyInspector);
  const [projectName, setProjectName] = useState("");
  const [workflowText, setWorkflowText] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [busyLabel, setBusyLabel] = useState("");
  const [error, setError] = useState("");
  const [workflowId, setWorkflowId] = useState<string | null>(null);
  const [workflowDetail, setWorkflowDetail] = useState<WorkflowDetail | null>(null);
  const [draft, setDraft] = useState<string>("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const refreshMemory = useCallback(async () => {
    const data = await listMemory(sessionId);
    setMemoryItems(data.items);
  }, [sessionId]);

  function onNewSession() {
    const d = new Date();
    const date = d.toISOString().slice(0, 10);
    const time = `${String(d.getHours()).padStart(2, "0")}${String(d.getMinutes()).padStart(2, "0")}`;
    setSessionId(`porto-${date}-${time}`);
    setMode("chat");
    setView("workbench");
  }

  function onNewWorkflow() {
    setWorkflowId(null);
    setWorkflowDetail(null);
    setDraft("");
    setProjectName("");
    setWorkflowText("");
    setSelectedFile(null);
    setMode("workflow");
    setView("workbench");
  }

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
  }, [workflowId, workflowDetail?.status]);

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
        setDocumentConfig(settingsResult.value.document);
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
  // 失败时切换 backendOnline=false，触发顶部离线横幅 + 禁用 Composer
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const snap = await getHealth();
        if (active) {
          setHealth(snap);
          setBackendOnline(true);
        }
      } catch {
        if (active) setBackendOnline(false);
      }
      if (active) timer = setTimeout(poll, 15000);
    }
    poll();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, []);

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

  async function saveDocumentConfig(nextConfig: DocumentConfig) {
    setBusyLabel("保存文件解析设置");
    setError("");
    try {
      const saved = await saveAppSettings({ document: nextConfig });
      setDocumentConfig(saved.document);
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存文件解析设置失败");
      return false;
    } finally {
      setBusyLabel("");
    }
  }

  const handleChatStart = useCallback(() => {
    setBusyLabel("生成回答");
    setError("");
    setInspector(emptyInspector);
  }, []);

  const handleChatFinish = useCallback(() => {
    setBusyLabel("");
    void refreshMemory();
  }, [refreshMemory]);

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
      setWorkflowRefreshKey((k) => k + 1);
      setDraft("");
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

  async function onSaveSpec(name: string, body: string): Promise<boolean> {
    if (!workflowId) return false;
    setError("");
    try {
      const detail = await updateWorkflowSpec(workflowId, name, body);
      setWorkflowDetail(detail);
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存规格失败");
      return false;
    }
  }

  async function onRerunStep(step: WorkflowStepName) {
    if (!workflowId) return;
    setBusyLabel("重跑本步");
    setError("");
    try {
      const resp = await rerunStep(workflowId, step);
      // rerun 异步在后台 worker 跑;立即拉一次详情触发 running 轮询分支,
      // 让 status=running 与后续 detail 变化进入既有 polling 循环。
      const detail = await getWorkflow(resp.workflow_id);
      setWorkflowDetail(detail);
    } catch (err) {
      setError(err instanceof Error ? err.message : "重跑失败");
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
    <div className="flex h-screen flex-col overflow-hidden">
        {!backendOnline ? (
          <div className="flex items-center justify-center gap-2 bg-rose-600 px-4 py-2 text-sm font-medium text-white">
            <Loader2 size={14} className="animate-spin" />
            后端未连接，请检查 make status
          </div>
        ) : null}
        <div className="grid min-h-0 flex-1 grid-cols-1 bg-zinc-100 text-zinc-950 lg:grid-cols-[300px_minmax(0,1fr)_380px]">
        <Sidebar
          busy={Boolean(busyLabel)}
          kbStats={kbStats}
          mode={mode}
          onNewSession={onNewSession}
          onNewWorkflow={onNewWorkflow}
          onPickWorkflow={onPickWorkflow}
          sessionId={sessionId}
          view={view}
          workflowId={workflowId}
          workflowRefreshKey={workflowRefreshKey}
          setMode={setMode}
          setSessionId={setSessionId}
          setView={setView}
        />

        <main className="flex min-h-0 min-w-0 flex-col overflow-hidden border-x border-zinc-200 bg-white">
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
              documentConfig={documentConfig}
              error={error}
              health={health}
              kbStats={kbStats}
              ragConfig={ragConfig}
              onRefreshIndex={refreshIndex}
              onSaveAgent={saveAgentConfig}
              onSaveDocument={saveDocumentConfig}
              onSaveRag={saveRagConfig}
            />
          ) : mode === "chat" ? (
            <ChatLoader
              key={sessionId}
              sessionId={sessionId}
              ragConfig={ragConfig}
              agentConfig={agentConfig}
              error={error}
              disabled={!backendOnline}
              onInspector={(s) => setInspector(s)}
              onError={(msg) => {
                setError(msg);
                setBusyLabel("");
              }}
              onStart={handleChatStart}
              onFinish={handleChatFinish}
            />
          ) : (
            <WorkflowPanel
              busy={Boolean(busyLabel)}
              detail={workflowDetail}
              draft={draft}
              error={error}
              fileInputRef={fileInputRef}
              onAdvance={onAdvance}
              onRun={runWorkflowAction}
              onRerunStep={onRerunStep}
              onSaveStep={onSaveStep}
              onSaveSpec={onSaveSpec}
              onTextChange={setWorkflowText}
              projectName={projectName}
              selectedFile={selectedFile}
              setDraft={setDraft}
              setProjectName={setProjectName}
              setSelectedFile={setSelectedFile}
              text={workflowText}
              workflowId={workflowId}
              agentMaxToolTurns={agentConfig.agent_max_tool_turns}
            />
          )}
        </main>

        <Inspector inspector={inspector} memoryItems={memoryItems} />
        </div>
      </div>
  );
}

type ChatUIMessage = {
  id: string;
  role: "user" | "assistant";
  parts: { type: "text"; text: string }[];
};

const WELCOME_MESSAGE: ChatUIMessage = {
  id: "porto-welcome",
  role: "assistant",
  parts: [{ type: "text", text: WELCOME_TEXT }],
};

// ChatLoader: key={sessionId} 保证切 session 时 remount → 重新加载该 session 历史。
// history 就绪后才渲染 ChatSession（mount useChatRuntime），让历史作为初始 messages。
// 不能用 reset/importExternalState：useExternalStoreRuntime 的外部 store 双向同步会
// 把 reset 创建的消息（无绑定的原始 Vercel message）过滤掉 → thread.messages=0。
function ChatLoader({
  sessionId,
  ragConfig,
  agentConfig,
  error,
  disabled,
  onInspector,
  onError: onErrorCb,
  onStart,
  onFinish,
}: {
  sessionId: string;
  ragConfig: RagConfig;
  agentConfig: AgentConfig;
  error: string;
  disabled: boolean;
  onInspector: (state: InspectorState) => void;
  onError: (msg: string) => void;
  onStart: () => void;
  onFinish: () => void;
}) {
  const [initialMessages, setInitialMessages] = useState<ChatUIMessage[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    listMemory(sessionId)
      .then((data) => {
        if (cancelled) return;
        const history: ChatUIMessage[] = [...data.items]
          .reverse()
          .filter((m) => m.role === "user" || m.role === "assistant")
          .map((m) => ({
            id: m.id,
            role: m.role as "user" | "assistant",
            parts: [{ type: "text", text: m.content }],
          }));
        setInitialMessages([WELCOME_MESSAGE, ...history]);
      })
      .catch(() => {
        if (!cancelled) setInitialMessages([WELCOME_MESSAGE]);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  if (!initialMessages) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-zinc-400">
        加载会话…
      </div>
    );
  }
  return (
    <ChatSession
      sessionId={sessionId}
      ragConfig={ragConfig}
      agentConfig={agentConfig}
      initialMessages={initialMessages}
      error={error}
      disabled={disabled}
      onInspector={onInspector}
      onError={onErrorCb}
      onStart={onStart}
      onFinish={onFinish}
    />
  );
}

function ChatSession({
  sessionId,
  ragConfig,
  agentConfig,
  initialMessages,
  error,
  disabled,
  onInspector,
  onError: onErrorCb,
  onStart,
  onFinish,
}: {
  sessionId: string;
  ragConfig: RagConfig;
  agentConfig: AgentConfig;
  initialMessages: ChatUIMessage[];
  error: string;
  disabled: boolean;
  onInspector: (state: InspectorState) => void;
  onError: (msg: string) => void;
  onStart: () => void;
  onFinish: () => void;
}) {
  // 附件 state 提升到此处：Composer 选择文件 → 立即上传 → 拿到 file_id 后
  // 写入 attachments；transport body 函数发送时读取 ref.current 拼到请求体。
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  // transport body 是 Resolvable<object>，可为函数；用 ref 让 body 函数每次发送
  // 都读取最新 file_ids，而无需重建 transport / runtime。
  const attachmentFileIdsRef = useRef<string[]>([]);

  // ref 与 state 同步：让 transport body 函数能拿到当前已上传成功的 file_id 列表。
  useEffect(() => {
    attachmentFileIdsRef.current = attachments
      .filter((a) => a.status === "done" && a.fileId)
      .map((a) => a.fileId as string);
  }, [attachments]);

  const uploadAttachments = useCallback(
    async (files: FileList | File[]) => {
      const incoming = Array.from(files);
      const newItems: ChatAttachment[] = incoming.map((file, idx) => ({
        id: `${file.name}-${file.size}-${Date.now()}-${idx}`,
        file,
        status: "uploading" as ChatAttachmentStatus,
      }));
      setAttachments((prev) => [...prev, ...newItems]);
      await Promise.all(
        newItems.map(async (item) => {
          try {
            const res = await uploadChatFile(item.file, sessionId);
            setAttachments((prev) =>
              prev.map((a) =>
                a.id === item.id
                  ? {
                      ...a,
                      status: "done" as ChatAttachmentStatus,
                      fileId: res.file_id,
                      pageCount: res.page_count,
                    }
                  : a,
              ),
            );
          } catch (err) {
            const msg = err instanceof Error ? err.message : "上传失败";
            setAttachments((prev) =>
              prev.map((a) =>
                a.id === item.id
                  ? { ...a, status: "error" as ChatAttachmentStatus, error: msg }
                  : a,
              ),
            );
            onErrorCb(`附件 ${item.file.name} 上传失败：${msg}`);
          }
        }),
      );
    },
    [onErrorCb, sessionId],
  );

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const clearAttachments = useCallback(() => setAttachments([]), []);

  const transport = useMemo(
    () =>
      new AssistantChatTransport({
        api: "/api/chat/stream",
        // body 为函数：每次发送消息时都会读取最新的 attachmentFileIdsRef。
        body: () => ({
          session_id: sessionId,
          rag: ragConfig,
          agent: agentConfig,
          top_k: ragConfig.top_k,
          file_ids: attachmentFileIdsRef.current,
        }),
        fetch(input, init) {
          onStart();
          // 此时 init.body 已是 JSON 字符串（ai-sdk 在调用 fetch 前序列化），
          // 可以安全清空附件 UI；避免发送后已选文件仍显示在输入框上方。
          clearAttachments();
          return globalThis.fetch(input, init);
        },
      }),
    [agentConfig, clearAttachments, onStart, ragConfig, sessionId],
  );
  const runtime = useChatRuntime({
    transport,
    messages: initialMessages,
    onData(dataPart) {
      if (dataPart.type !== "data-porto") return;
      if (isInspectorState(dataPart.data)) onInspector(dataPart.data);
    },
    onError(err) {
      onErrorCb(err instanceof Error ? err.message : "请求失败");
    },
    onFinish,
  });
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadView
        error={error}
        disabled={disabled}
        attachments={attachments}
        onUploadAttachments={uploadAttachments}
        onRemoveAttachment={removeAttachment}
      />
    </AssistantRuntimeProvider>
  );
}

function Sidebar({
  busy,
  kbStats,
  mode,
  onNewSession,
  onNewWorkflow,
  onPickWorkflow,
  sessionId,
  view,
  workflowId,
  workflowRefreshKey,
  setMode,
  setSessionId,
  setView,
}: {
  busy: boolean;
  kbStats: KbStats | null;
  mode: Mode;
  onNewSession: () => void;
  onNewWorkflow: () => void;
  onPickWorkflow: (id: string) => void;
  sessionId: string;
  view: View;
  workflowId: string | null;
  workflowRefreshKey: number;
  setMode: (value: Mode) => void;
  setSessionId: (value: string) => void;
  setView: (value: View) => void;
}) {
  return (
    <aside className="min-h-0 min-w-0 overflow-y-auto border-b border-zinc-200 bg-zinc-50 p-4 lg:border-b-0">
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

      <div className="mb-4 grid grid-cols-2 gap-2">
        <button
          className="flex items-center justify-center gap-1 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-700 hover:bg-zinc-100"
          onClick={onNewSession}
          title="开始新会话"
        >
          <Plus size={14} />
          新会话
        </button>
        <button
          className="flex items-center justify-center gap-1 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-700 hover:bg-zinc-100"
          onClick={onNewWorkflow}
          title="开始新拆解"
        >
          <Plus size={14} />
          新拆解
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

      <SessionList
        activeSessionId={sessionId}
        onPickSession={(sid) => {
          setSessionId(sid);
          setMode("chat");
          setView("workbench");
        }}
      />

      <WorkflowList
        activeWorkflowId={workflowId}
        onPickWorkflow={onPickWorkflow}
        refreshKey={workflowRefreshKey}
      />
    </aside>
  );
}

function ThreadView({
  error,
  disabled,
  attachments,
  onUploadAttachments,
  onRemoveAttachment,
}: {
  error: string;
  disabled: boolean;
  attachments: ChatAttachment[];
  onUploadAttachments: (files: FileList | File[]) => Promise<void>;
  onRemoveAttachment: (id: string) => void;
}) {
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
        <Composer
          disabled={disabled}
          attachments={attachments}
          onUploadAttachments={onUploadAttachments}
          onRemoveAttachment={onRemoveAttachment}
        />
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
    <MarkdownView
      value={text}
      className="prose prose-zinc max-w-none prose-pre:rounded-lg prose-pre:bg-zinc-950 prose-pre:text-zinc-50"
    />
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

function Composer({
  disabled,
  attachments,
  onUploadAttachments,
  onRemoveAttachment,
}: {
  disabled: boolean;
  attachments: ChatAttachment[];
  onUploadAttachments: (files: FileList | File[]) => Promise<void>;
  onRemoveAttachment: (id: string) => void;
}) {
  // disabled 时同时禁止上传（与输入框行为一致）。
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handlePick = () => {
    if (disabled) return;
    fileInputRef.current?.click();
  };

  return (
    <ComposerPrimitive.Root className="mx-auto flex max-w-4xl flex-col gap-2">
      {attachments.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {attachments.map((a) => (
            <span
              key={a.id}
              className="inline-flex items-center gap-1.5 rounded-md border border-zinc-200 bg-zinc-50 px-2 py-1 text-xs text-zinc-700"
              title={
                a.status === "done" && a.pageCount != null
                  ? `${a.pageCount} 页`
                  : a.status === "error"
                    ? a.error
                    : undefined
              }
            >
              <span className="max-w-[12rem] truncate">{a.file.name}</span>
              {a.status === "uploading" ? (
                <Loader2 size={12} className="animate-spin text-zinc-400" />
              ) : a.status === "error" ? (
                <span className="text-rose-600" title={a.error}>
                  !
                </span>
              ) : (
                <span className="text-zinc-400">{a.pageCount}p</span>
              )}
              <button
                type="button"
                aria-label={`移除附件 ${a.file.name}`}
                className="text-zinc-400 hover:text-zinc-700"
                onClick={() => onRemoveAttachment(a.id)}
              >
                <X size={12} />
              </button>
            </span>
          ))}
        </div>
      ) : null}
      <div className="flex items-end gap-2">
        <input
          ref={fileInputRef}
          className="hidden"
          type="file"
          multiple
          accept=".pdf,.docx,.md,.txt"
          onChange={(event) => {
            const files = event.target.files;
            if (files && files.length > 0) {
              void onUploadAttachments(files);
            }
            // 重置 value 让用户能再次选择同一文件（否则 change 事件不会触发）。
            event.target.value = "";
          }}
        />
        <button
          type="button"
          onClick={handlePick}
          disabled={disabled}
          className="flex size-12 shrink-0 items-center justify-center rounded-xl border border-zinc-200 bg-white text-zinc-600 transition hover:bg-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
          title="上传附件（PDF / Word / Markdown / Text）"
          aria-label="上传附件"
        >
          <Upload size={17} />
        </button>
        <ComposerPrimitive.Input
          className="min-h-12 flex-1 resize-none rounded-xl border border-zinc-200 bg-white px-3 py-3 text-sm outline-none focus:border-zinc-400 disabled:opacity-50"
          placeholder="询问 ~/.scv/analysis 知识库..."
          rows={1}
          disabled={disabled}
        />
        <ComposerPrimitive.Send
          className="flex size-12 shrink-0 items-center justify-center rounded-xl bg-zinc-950 text-white transition hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={disabled}
        >
          <Send size={17} />
        </ComposerPrimitive.Send>
      </div>
    </ComposerPrimitive.Root>
  );
}

function SettingsPage({
  agentConfig,
  busy,
  documentConfig,
  error,
  health,
  kbStats,
  ragConfig,
  onRefreshIndex,
  onSaveAgent,
  onSaveDocument,
  onSaveRag,
}: {
  agentConfig: AgentConfig;
  busy: boolean;
  documentConfig: DocumentConfig;
  error: string;
  health: HealthSnapshot | null;
  kbStats: KbStats | null;
  ragConfig: RagConfig;
  onRefreshIndex: (config?: RagConfig) => Promise<void>;
  onSaveAgent: (config: AgentConfig) => Promise<void>;
  onSaveDocument: (config: DocumentConfig) => Promise<boolean>;
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
              id: "document" as const,
              label: "文件解析",
              icon: <FileInput size={15} />,
            },
            {
              id: "knowledge" as const,
              label: "Knowledge",
              icon: <Database size={15} />,
            },
            {
              id: "architecture" as const,
              label: "架构",
              icon: <Network size={15} />,
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

        {section === "document" ? (
          <DocumentSettingsForm
            key={JSON.stringify(documentConfig)}
            busy={busy}
            documentConfig={documentConfig}
            onSaved={() => markSaved("文件解析设置已保存")}
            onSaveDocument={onSaveDocument}
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

        {section === "architecture" ? <ArchitectureView /> : null}
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

function DocumentSettingsForm({
  busy,
  documentConfig,
  onSaved,
  onSaveDocument,
}: {
  busy: boolean;
  documentConfig: DocumentConfig;
  onSaved: () => void;
  onSaveDocument: (config: DocumentConfig) => Promise<boolean>;
}) {
  const [documentDraft, setDocumentDraft] =
    useState<DocumentConfig>(documentConfig);

  const updateDocument = <K extends keyof DocumentConfig>(
    key: K,
    value: DocumentConfig[K],
  ) => {
    setDocumentDraft((current) => ({ ...current, [key]: value }));
  };

  async function saveDocument() {
    if (await onSaveDocument(documentDraft)) onSaved();
  }

  const modeDescription = {
    hybrid: "优先调用当前 Agent 模型理解 PDF 视觉内容，失败时自动回退本地解析。",
    local: "文件不会发送给大模型，只使用本地解析器。适合隐私敏感环境。",
    native: "强制使用大模型原生 PDF 能力；模型不支持或调用失败时拒绝上传。",
  }[documentDraft.parse_mode];

  return (
    <SettingsCard
      busy={busy}
      title="文件解析"
      onSave={saveDocument}
    >
      <div className="rounded-lg border border-sky-200 bg-sky-50 p-3">
        <p className="text-sm font-medium text-sky-900">PRD 解析策略</p>
        <p className="mt-1 text-xs leading-5 text-sky-700">{modeDescription}</p>
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <label className="block">
          <span className="text-xs text-zinc-500">解析模式</span>
          <select
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            value={documentDraft.parse_mode}
            onChange={(event) =>
              updateDocument(
                "parse_mode",
                event.target.value as DocumentConfig["parse_mode"],
              )
            }
          >
            <option value="hybrid">Hybrid（推荐）</option>
            <option value="local">Local（仅本地）</option>
            <option value="native">Native（严格模型解析）</option>
          </select>
        </label>

        <label className="block">
          <span className="text-xs text-zinc-500">本地 PDF 解析器</span>
          <select
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            value={documentDraft.local_parser}
            onChange={(event) =>
              updateDocument(
                "local_parser",
                event.target.value as DocumentConfig["local_parser"],
              )
            }
          >
            <option value="pypdf">pypdf（轻量文本提取）</option>
            <option value="docling">Docling（OCR / 表格 / 布局）</option>
          </select>
        </label>

        <label className="block">
          <span className="text-xs text-zinc-500">模型解析最大输出 Tokens</span>
          <input
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            max={128000}
            min={1000}
            step={1000}
            type="number"
            value={documentDraft.max_tokens}
            onChange={(event) =>
              updateDocument("max_tokens", Number(event.target.value))
            }
          />
          <span className="mt-1 block text-xs text-zinc-400">
            仅 Native / Hybrid 的模型 PDF 解析使用
          </span>
        </label>

        <label className="block">
          <span className="text-xs text-zinc-500">单文件上传上限（MB）</span>
          <input
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            max={200}
            min={1}
            type="number"
            value={documentDraft.max_upload_mb}
            onChange={(event) =>
              updateDocument("max_upload_mb", Number(event.target.value))
            }
          />
        </label>

        <label className="block">
          <span className="text-xs text-zinc-500">PDF 最大页数</span>
          <input
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
            max={1000}
            min={1}
            type="number"
            value={documentDraft.max_pdf_pages}
            onChange={(event) =>
              updateDocument("max_pdf_pages", Number(event.target.value))
            }
          />
        </label>
      </div>

      <div className="mt-5 rounded-lg border border-zinc-200 bg-zinc-50 p-3 text-xs leading-5 text-zinc-500">
        <p>
          <span className="font-medium text-zinc-700">Docling：</span>
          后端需安装 document-ai extra，否则选择 Docling 后上传会返回明确错误。
        </p>
        <p className="mt-1">
          Markdown 中的远程图片不会自动下载；相对图片需要后续通过资源包一起上传。
        </p>
      </div>
    </SettingsCard>
  );
}

// Claude Agent SDK 选中时，模型下拉仅允许这些 Claude 模型。
// 与后端 anthropic provider 默认模型对齐。
const CLAUDE_MODELS = [
  "claude-sonnet-5",
  "claude-opus-5",
  "claude-haiku-4-5",
  "claude-sonnet-4-6",
  "claude-opus-4-8",
] as const;

type EngineBackend = "langchain" | "agent_sdk";

// 引擎卡片选择器：渲染一组互斥的 backend 选项（Langchain / Claude Agent SDK）。
// 选中卡片用 border + CheckCircle2 标记，匹配 Settings 页既有圆角/边框风格。
function EngineCardGroup({
  title,
  description,
  value,
  options,
  onChange,
  busy,
}: {
  title: string;
  description: string;
  value: EngineBackend;
  options: Array<{ value: EngineBackend; label: string; hint?: string }>;
  onChange: (value: EngineBackend) => void;
  busy: boolean;
}) {
  return (
    <div>
      <p className="text-sm font-semibold text-zinc-700">{title}</p>
      <p className="text-xs text-zinc-400">{description}</p>
      <div className="mt-2 grid gap-3 sm:grid-cols-2">
        {options.map((opt) => {
          const selected = value === opt.value;
          return (
            <button
              type="button"
              key={opt.value}
              disabled={busy}
              aria-pressed={selected}
              onClick={() => onChange(opt.value)}
              className={`relative flex flex-col items-start rounded-lg border p-3 text-left transition disabled:cursor-not-allowed ${
                selected
                  ? "border-zinc-950 bg-zinc-50 ring-1 ring-zinc-950"
                  : "border-zinc-200 hover:border-zinc-300 hover:bg-zinc-50"
              }`}
            >
              {selected ? (
                <CheckCircle2
                  size={16}
                  className="absolute right-2 top-2 text-zinc-950"
                />
              ) : null}
              <span className="text-sm font-medium text-zinc-800">
                {opt.label}
              </span>
              {opt.hint ? (
                <span className="mt-1 text-xs text-zinc-400">{opt.hint}</span>
              ) : null}
            </button>
          );
        })}
      </div>
    </div>
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

  // provider 联动：任一 backend 选中 agent_sdk → 锁定 provider=anthropic、
  // 模型仅限 Claude 系列。切回 langchain 时恢复自由选择。
  const providerLocked =
    agentDraft.chatbot_backend === "agent_sdk" ||
    agentDraft.workflow_backend === "agent_sdk";

  function handleBackendChange(
    kind: "chatbot" | "workflow",
    value: EngineBackend,
  ) {
    setAgentDraft((current) => {
      const next: AgentConfig = {
        ...current,
        [kind === "chatbot" ? "chatbot_backend" : "workflow_backend"]: value,
      };
      const locked =
        next.chatbot_backend === "agent_sdk" ||
        next.workflow_backend === "agent_sdk";
      if (locked) {
        next.agent_provider = "anthropic";
        // 当前模型不在 Claude 白名单 → 回退到推荐默认
        if (!(CLAUDE_MODELS as readonly string[]).includes(next.agent_model)) {
          next.agent_model = CLAUDE_MODELS[0];
        }
      }
      return next;
    });
  }

  async function saveAgent() {
    await onSaveAgent(agentDraft);
    onSaved();
  }

  return (
    <SettingsCard busy={busy} title="Agent Settings" onSave={saveAgent}>
      {/* 引擎选择：置于 agent 设置顶部 */}
      <div className="space-y-4">
        <EngineCardGroup
          title="Chatbot 引擎"
          description="知识库问答所使用的后端"
          value={agentDraft.chatbot_backend ?? "langchain"}
          busy={busy}
          onChange={(v) => handleBackendChange("chatbot", v)}
          options={[
            {
              value: "langchain",
              label: "Langchain RAG",
              hint: "基于检索增强生成的问答",
            },
            {
              value: "agent_sdk",
              label: "Claude Agent SDK",
              hint: "Anthropic 原生 Agent（仅 Claude）",
            },
          ]}
        />
        <EngineCardGroup
          title="Workflow 引擎"
          description="PRD 拆解工作流所使用的后端"
          value={agentDraft.workflow_backend ?? "langchain"}
          busy={busy}
          onChange={(v) => handleBackendChange("workflow", v)}
          options={[
            {
              value: "langchain",
              label: "Langchain",
              hint: "自由选择 provider / 模型",
            },
            {
              value: "agent_sdk",
              label: "Claude Agent SDK",
              hint: "Anthropic 原生 Agent（仅 Claude）",
            },
          ]}
        />
        {providerLocked ? (
          <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
            已启用 Claude Agent SDK：Provider 已锁定为 anthropic，模型仅可选择 Claude
            系列。
          </div>
        ) : null}
      </div>

      <div className="my-5 h-px bg-zinc-200" />

      <div className="grid gap-4 md:grid-cols-2">
        <label className="block">
          <span className="text-xs text-zinc-500">
            Provider{providerLocked ? "（已锁定）" : ""}
          </span>
          <select
            className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm disabled:cursor-not-allowed disabled:bg-zinc-100 disabled:text-zinc-400"
            value={agentDraft.agent_provider}
            disabled={providerLocked}
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
          <span className="text-xs text-zinc-500">
            Model{providerLocked ? "（仅 Claude）" : ""}
          </span>
          {providerLocked ? (
            <select
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              value={agentDraft.agent_model}
              onChange={(event) =>
                updateAgent("agent_model", event.target.value)
              }
            >
              {CLAUDE_MODELS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          ) : (
            <input
              className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-2 text-sm"
              value={agentDraft.agent_model}
              onChange={(event) =>
                updateAgent("agent_model", event.target.value)
              }
            />
          )}
        </label>
        <label className="block md:col-span-2">
          <span className="text-xs text-zinc-500">
            Base URL
            {providerLocked ? (
              <span className="ml-1 text-amber-600">
                （Agent SDK：作为 ANTHROPIC_BASE_URL 传递给 Claude Code CLI）
              </span>
            ) : null}
          </span>
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
          <span className="text-xs text-zinc-500">
            API Key
            {providerLocked ? (
              <span className="ml-1 text-amber-600">
                （Agent SDK：作为 ANTHROPIC_API_KEY 传递给 Claude Code CLI）
              </span>
            ) : null}
          </span>
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
                updateAgent(
                  "spec_refine_concurrency",
                  Math.min(10, Math.max(1, Number(event.target.value))),
                )
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
  onRerunStep,
  onSaveStep,
  onSaveSpec,
  onTextChange,
  projectName,
  selectedFile,
  setDraft,
  setProjectName,
  setSelectedFile,
  text,
  workflowId,
  agentMaxToolTurns,
}: {
  busy: boolean;
  detail: WorkflowDetail | null;
  draft: string;
  error: string;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  onAdvance: () => void;
  onRun: () => void;
  onRerunStep: (step: WorkflowStepName) => Promise<void>;
  onSaveStep: (step: WorkflowStepName, output: Record<string, unknown>) => void;
  onSaveSpec: (name: string, body: string) => Promise<boolean>;
  onTextChange: (value: string) => void;
  projectName: string;
  selectedFile: File | null;
  setDraft: (value: string) => void;
  setProjectName: (value: string) => void;
  setSelectedFile: (value: File | null) => void;
  text: string;
  workflowId: string | null;
  agentMaxToolTurns: number;
}) {
  const curStep = detail?.current_step ?? null;
  const curIdx = curStep ? WORKFLOW_STEPS.indexOf(curStep) : -1;
  const showCheckpoint =
    !!detail &&
    detail.status === "awaiting_input" &&
    !!curStep &&
    CHECKPOINT_STEPS.includes(curStep);

  return (
    <div className="flex min-h-0 flex-1 flex-col p-4">
      {error ? (
        <div className="mb-3 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </div>
      ) : null}

      {!workflowId && !detail ? (
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

      {/* workflowId 已设置但首次轮询/加载尚未返回 detail —— 显示加载态，
          防止此时输入表单 + 运行按钮可用导致重复创建工作流。 */}
      {workflowId && !detail ? (
        <div className="my-6 flex items-center gap-2 text-sm text-zinc-500">
          <Loader2 className="animate-spin" size={16} />
          加载工作流…
        </div>
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

          <StepRerunToolbar
            detail={detail}
            agentMaxToolTurns={agentMaxToolTurns}
            onRerunStep={onRerunStep}
          />

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
            <CompletedView detail={detail} onSaveSpec={onSaveSpec} />
          ) : null}

          {/* 未显式覆盖的状态（interrupted / created / 其他）：显示中性横幅 + 继续/重试按钮，
              使服务重启后的 interrupted 工作流也能被 resume。 */}
          {detail.status !== "running" &&
          detail.status !== "failed" &&
          detail.status !== "completed" &&
          !showCheckpoint ? (
            <div className="my-4 rounded-md border border-zinc-200 bg-zinc-50 p-3 text-sm text-zinc-700">
              工作流状态: {detail.status}
              <button
                className="ml-2 underline"
                onClick={onAdvance}
                type="button"
              >
                继续/重试
              </button>
            </div>
          ) : null}
        </>
      ) : null}

      <div className="mt-3 flex items-center justify-end gap-3">
        <button
          className="flex items-center gap-2 rounded-lg bg-zinc-950 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-800 disabled:opacity-40"
          disabled={
            busy ||
            (!!workflowId && !detail) ||
            (!text.trim() && !selectedFile)
          }
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

/**
 * 截断 step 重跑工具栏。
 * - generate 步:any subsystem truncated 时显示「{n}/{total} 子系统超限」chip + 重跑按钮
 * - understand/identify 单步 truncated 时仅显示重跑按钮
 * - 三态:enabled(blue) / loading(gray + spinner) / at-cap disabled(gray,引导手编)
 *
 * 后端 T7 的 rerun 把 cur ×1.5 cap 到 hard_cap(40),撞顶 → 409,所以按钮这里也按
 * curMax>=hard_cap 预禁用,避免无谓请求。turn 变化只放 tooltip,不挤进按钮文本。
 */
const RERUN_STEPS: WorkflowStepName[] = ["understand", "identify", "generate"];

function StepRerunToolbar({
  detail,
  agentMaxToolTurns,
  onRerunStep,
}: {
  detail: WorkflowDetail;
  agentMaxToolTurns: number;
  onRerunStep: (step: WorkflowStepName) => Promise<void>;
}) {
  const [rerunning, setRerunning] = useState<WorkflowStepName | null>(null);

  const truncatedSteps = RERUN_STEPS.filter(
    (step) => !!readStepToolMeta(detail.outputs[step])?.truncated,
  );

  const specResults = readSpecResults(detail);
  const allSpecNames = Object.keys(specResults);
  const truncSpecCount = allSpecNames.filter(
    (n) => !!specResults[n]?.tool_meta?.truncated,
  ).length;

  const curMax = agentMaxToolTurns;
  const newMax = Math.min(Math.ceil(curMax * 1.5), TOOL_TURN_HARD_CAP);
  const atCap = curMax >= TOOL_TURN_HARD_CAP;

  if (truncatedSteps.length === 0) return null;

  async function handleRerun(step: WorkflowStepName) {
    setRerunning(step);
    try {
      await onRerunStep(step);
    } finally {
      setRerunning(null);
    }
  }

  return (
    <div className="my-3 flex flex-wrap items-center gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs">
      <span className="font-medium text-amber-900">工具调用截断</span>
      {truncatedSteps.map((step) => {
        const isRerunning = rerunning === step;
        const label = STEP_LABELS[step];
        const title = atCap
          ? `已达 turn 硬上限(${TOOL_TURN_HARD_CAP}),请检查 prompt 或手动编辑产出`
          : `${curMax}→${newMax} turn · 上限 ${TOOL_TURN_HARD_CAP}`;
        const buttonClass = isRerunning
          ? "cursor-not-allowed bg-gray-400 text-white"
          : atCap
            ? "cursor-not-allowed bg-gray-100 text-gray-500"
            : "bg-blue-600 text-white hover:bg-blue-700";
        return (
          <span className="flex items-center gap-2" key={step}>
            {step === "generate" && allSpecNames.length > 0 ? (
              <span className="rounded-full border border-red-300 bg-red-50 px-2 py-0.5 text-red-700">
                {truncSpecCount}/{allSpecNames.length} 子系统超限
              </span>
            ) : null}
            <button
              className={`flex items-center gap-1 rounded-md border border-transparent px-2.5 py-1 text-xs font-medium ${buttonClass}`}
              disabled={isRerunning || atCap}
              onClick={() => void handleRerun(step)}
              title={title}
              type="button"
            >
              {isRerunning ? (
                <>
                  <Loader2 size={12} className="animate-spin" />
                  重跑中…
                </>
              ) : atCap ? (
                `重跑${label} · 已达上限`
              ) : (
                <>
                  <span aria-hidden>⟳</span>
                  重跑{label}
                </>
              )}
            </button>
          </span>
        );
      })}
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
  // 默认预览(AI 产出先看),一个开关切到分栏编辑
  const [editing, setEditing] = useState(false);
  return (
    <div className="my-4 flex min-h-0 flex-1 flex-col rounded-xl border border-zinc-200 bg-white">
      <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        <button
          className={`rounded-md px-2 py-1 text-xs ${editing ? "text-zinc-600 hover:bg-zinc-100" : "bg-zinc-950 text-white"}`}
          onClick={() => setEditing((e) => !e)}
          type="button"
        >
          {editing ? "完成编辑" : "编辑"}
        </button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-4">
        {editing ? (
          <SpecMdxEditor
            value={draft}
            onChange={setDraft}
            preview="live"
            className="min-h-0 flex-1"
          />
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto">
            <MarkdownView value={draft} />
          </div>
        )}
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-zinc-200 px-4 py-2">
        <button
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm text-zinc-700 hover:bg-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={!draft.trim()}
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

function SpecActionButton({
  children,
  disabled,
  onClick,
  primary = false,
}: {
  children: React.ReactNode;
  disabled?: boolean;
  onClick: (e: React.MouseEvent) => void;
  primary?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={`flex items-center gap-1 rounded px-2 py-1 text-xs ${
        primary
          ? "bg-zinc-950 text-white hover:bg-zinc-800"
          : "text-zinc-600 hover:bg-zinc-200"
      } disabled:opacity-40`}
    >
      {children}
    </button>
  );
}

function SpecCard({
  name,
  body,
  onSave,
  toolMeta,
}: {
  name: string;
  body: string;
  onSave: (name: string, body: string) => Promise<boolean>;
  toolMeta?: ToolMeta;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(body);
  const [saving, setSaving] = useState(false);
  const truncated = !!toolMeta?.truncated;

  function startEdit(e: React.MouseEvent) {
    e.stopPropagation();
    setDraft(body);
    setEditing(true);
    setOpen(true);
  }

  async function handleSave(e: React.MouseEvent) {
    e.stopPropagation();
    setSaving(true);
    try {
      const ok = await onSave(name, draft);
      if (ok) setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  function handleCancel(e: React.MouseEvent) {
    e.stopPropagation();
    setEditing(false);
    setDraft(body);
  }

  function handleDownload(e: React.MouseEvent) {
    e.stopPropagation();
    triggerDownload(`${sanitizeFilename(name)}.md`, body);
  }

  return (
    <details
      className={`rounded-lg border bg-zinc-50 ${
        truncated
          ? "border-zinc-200 border-l-4 border-l-red-500"
          : "border-zinc-200"
      }`}
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary className="flex cursor-pointer items-center justify-between gap-2 px-3 py-2">
        <span className="truncate text-sm font-medium">{name}</span>
        <span className="flex shrink-0 items-center gap-1">
          {editing ? (
            <>
              <SpecActionButton disabled={saving} onClick={handleSave} primary>
                {saving ? <Loader2 size={13} className="animate-spin" /> : null}
                保存
              </SpecActionButton>
              <SpecActionButton disabled={saving} onClick={handleCancel}>
                取消
              </SpecActionButton>
            </>
          ) : (
            <>
              <SpecActionButton disabled={saving} onClick={handleDownload}>
                <Download size={13} />
                下载
              </SpecActionButton>
              <SpecActionButton disabled={saving} onClick={startEdit}>
                <Pencil size={13} />
                编辑
              </SpecActionButton>
            </>
          )}
        </span>
      </summary>
      {editing ? (
        <div className="px-3 pb-3">
          <SpecMdxEditor
            value={draft}
            onChange={setDraft}
            preview="live"
            height={400}
          />
        </div>
      ) : (
        <div className="max-h-[60vh] overflow-y-auto px-3 pb-3">
          <MarkdownView value={body} />
        </div>
      )}
      {truncated && toolMeta ? (
        <div className="mx-3 mb-3 mt-2 flex items-center gap-2 border-t border-dashed border-red-300 pt-2 text-xs text-red-700">
          <span>
            ⚠️ 工具超限 {toolMeta.tool_calls ?? "?"}/{toolMeta.max_turns ?? "?"}
          </span>
          <span
            className="cursor-help border-b border-dotted border-red-700"
            title={`工具调用达上限 ${toolMeta.tool_calls ?? "?"}/${toolMeta.turns ?? "?"} turn · max_turns=${toolMeta.max_turns ?? "?"}`}
          >
            ⓘ 详情
          </span>
        </div>
      ) : null}
    </details>
  );
}

function CompletedView({
  detail,
  onSaveSpec,
}: {
  detail: WorkflowDetail;
  onSaveSpec: (name: string, body: string) => Promise<boolean>;
}) {
  const projectName = detail.project_name;
  const understanding = detail.outputs.understand?.output.understanding;
  const subsystems = readSubsystems(detail);
  const specs = detail.outputs.generate?.output.specs as
    | Record<string, string>
    | undefined;
  const specResults = readSpecResults(detail);
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
          <MarkdownView
            value={
              typeof understanding === "string"
                ? understanding
                : "_（无 understanding 输出）_"
            }
          />
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
            {specs && Object.keys(specs).length > 0 ? (
              <div className="flex justify-end">
                <button
                  type="button"
                  className="flex items-center gap-1 rounded-md border border-zinc-200 bg-white px-3 py-1.5 text-xs text-zinc-600 hover:bg-zinc-100"
                  onClick={() =>
                    triggerDownload(
                      `${sanitizeFilename(projectName || "specs")}.md`,
                      Object.entries(specs)
                        .map(([n, b]) => `# ${n}\n\n${b}`)
                        .join("\n\n---\n\n"),
                    )
                  }
                >
                  <Download size={13} />
                  下载全部
                </button>
              </div>
            ) : null}
            {specs
              ? Object.entries(specs).map(([name, specBody]) => (
                  <SpecCard
                    key={name}
                    name={name}
                    body={specBody}
                    onSave={onSaveSpec}
                    toolMeta={specResults[name]?.tool_meta}
                  />
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
