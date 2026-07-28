# Settings 架构图面板 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Settings 页面新增「架构」tab，用 6 张 mermaid 流程图展示 agent 的问话方式、context 组装、workflow loop。

**Architecture:** 纯前端改动。新建 `MermaidDiagram` 可复用渲染组件（client-side 动态 import mermaid，useEffect 内 render SVG）；新建 `ArchitectureView` 面板（6 张图硬编码为 TS 常量，分三组）；修改 `porto-workbench.tsx` 接入新 tab。

**Tech Stack:** Next.js 16 canary, React 19, mermaid (npm), TailwindCSS 4, lucide-react

## Global Constraints

- 前端目录：`frontend/`，所有路径相对该目录
- 无测试框架（无 vitest/jest）—— 验证靠 `npm run build`（类型/编译检查）+ `npm run dev` 手动视觉验证
- 项目 AGENTS.md 要求：写 Next.js 代码前查 `node_modules/next/dist/docs/` 确认 API
- 静态文档：图内容硬编码，不依赖运行时 config
- mermaid theme: `"default"`（light），与 app `data-color-mode="light"` 一致
- 中文 UI 文案（与现有 Settings 面板一致）

---

### Task 1: 安装 mermaid + 创建 MermaidDiagram 组件

**Files:**
- Create: `src/components/mermaid-diagram.tsx`

**Interfaces:**
- Produces: `MermaidDiagram({ chart: string; className?: string })` — 接收 mermaid 语法字符串，渲染 SVG 到 DOM

- [ ] **Step 1: 安装 mermaid 依赖**

```bash
cd frontend && npm install mermaid
```

- [ ] **Step 2: 查阅 Next.js 16 client component 文档**

AGENTS.md 要求。查看 `node_modules/next/dist/docs/01-app/` 下关于 client components 和 dynamic imports 的文档，确认 `"use client"` + `useEffect` 内 `import()` 模式在 Next.js 16 canary 中是否受支持（预计无变化，但需确认）。

- [ ] **Step 3: 创建 MermaidDiagram 组件**

Create `src/components/mermaid-diagram.tsx`:

```tsx
"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Loader2 } from "lucide-react";

/**
 * 可复用的 mermaid 流程图渲染组件。
 *
 * 在 useEffect 内动态 import("mermaid") —— 只在浏览器执行，
 * 不影响 SSR。mermaid.render() 返回 SVG 字符串，注入 ref 容器。
 * 初始化只执行一次（模块级 flag）。
 */

let mermaidInitialized = false;

export function MermaidDiagram({
  chart,
  className,
}: {
  chart: string;
  className?: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  // useId 返回含冒号的 ID（如 «r0»），mermaid render id 需合法 HTML id
  const rawId = useId();
  const renderId = `mmd-${rawId.replace(/[^a-zA-Z0-9]/g, "")}`;
  const [status, setStatus] = useState<"loading" | "done" | "error">("loading");

  useEffect(() => {
    let cancelled = false;

    async function renderChart() {
      try {
        const mermaid = (await import("mermaid")).default;
        if (!mermaidInitialized) {
          mermaid.initialize({
            startOnLoad: false,
            theme: "default",
            securityLevel: "loose",
          });
          mermaidInitialized = true;
        }
        const { svg } = await mermaid.render(renderId, chart);
        if (!cancelled && containerRef.current) {
          containerRef.current.innerHTML = svg;
          setStatus("done");
        }
      } catch {
        if (!cancelled) setStatus("error");
      }
    }

    setStatus("loading");
    renderChart();
    return () => {
      cancelled = true;
    };
  }, [chart, renderId]);

  if (status === "error") {
    return (
      <pre className="overflow-x-auto rounded-lg bg-zinc-50 p-3 text-xs text-zinc-600">
        {chart}
      </pre>
    );
  }

  return (
    <div className={className}>
      {status === "loading" ? (
        <div className="flex items-center gap-2 py-8 text-sm text-zinc-400">
          <Loader2 size={16} className="animate-spin" />
          渲染图中…
        </div>
      ) : null}
      <div
        ref={containerRef}
        className="overflow-x-auto"
        style={{ display: status === "done" ? "block" : "none" }}
      />
    </div>
  );
}
```

**设计要点：**
- `securityLevel: "loose"` — mermaid 默认 `strict` 会转义 HTML 标签（`<br/>` 在节点文本里会显示为字面量）。架构图节点含 `<br/>` 换行，需要 `loose` 才能正常渲染。
- `mermaidInitialized` 模块级 flag —— 多个 MermaidDiagram 实例共享一次 `initialize` 调用。
- 渲染失败 fallback：显示原始 chart 文本（`<pre>`），便于调试。
- loading/done 状态切换：done 时隐藏 spinner，显示 SVG 容器。

- [ ] **Step 4: 验证编译通过**

```bash
cd frontend && npm run build
```

Expected: build 成功，无 TypeScript 错误。如果报错检查 import 类型。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/mermaid-diagram.tsx frontend/package.json frontend/package-lock.json
git commit -m "feat(frontend): add MermaidDiagram component

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 创建 ArchitectureView 组件（6 张图）

**Files:**
- Create: `src/components/architecture-view.tsx`

**Interfaces:**
- Consumes: `MermaidDiagram` from Task 1（`import { MermaidDiagram } from "./mermaid-diagram"`）
- Produces: `ArchitectureView()` — 无 props，渲染完整架构面板

- [ ] **Step 1: 创建 ArchitectureView 组件**

Create `src/components/architecture-view.tsx`:

```tsx
"use client";

import { MermaidDiagram } from "./mermaid-diagram";

// ── 图 1：Intent router（问话方式）──
// 来源：docs/backend-agent-guide.md §6
const INTENT_ROUTER = `flowchart LR
    M[用户消息] --> Q{LLM 可用?}
    Q -->|是| LS[LLM complete_structured<br/>输出 intent: direct/rag]
    LS --> D[用 LLM 判断结果]
    Q -->|否| RS[规则: 正则 + 关键词]
    RS --> D`;

// ── 图 2：Streaming（问话方式）──
// 来源：§9
const STREAMING = `sequenceDiagram
    participant FE as 前端
    participant API as chat_stream
    participant LLM as 大模型
    FE->>API: POST /api/chat/stream
    API->>API: prep(意图/检索/记忆/拼prompt)
    loop LLM token 流
        LLM-->>API: delta token
        API-->>FE: SSE text-delta
    end
    API->>FE: SSE source-document / data-porto / finish`;

// ── 图 3：Memory 三层存储 + compaction（Context 组装）──
// 来源：§7
const MEMORY = `flowchart TB
    subgraph STOR["存储"]
        A[(SQLite<br/>每条消息原文)]
        B[(ChromaDB<br/>消息向量, 语义检索)]
        C[(SQLite<br/>session 摘要缓存)]
    end
    Msg[新消息] --> A
    Msg --> B
    Query[检索] --> B
    Query --> Hist[历史]
    Hist --> Check{条数 > 阈值?}
    Check -->|否| Out1[全部原文]
    Check -->|是| Split[旧消息 + 近期N条]
    Split --> Cache{摘要已缓存?<br/>by last_message_id}
    Cache -->|是| Reuse[复用摘要]
    Cache -->|否| Sum[LLM 摘要旧消息]
    Sum --> C
    Reuse --> Out2[摘要 + 近期原文]
    Sum --> Out2`;

// ── 图 4：Prompt 拼装顺序 + 预算截断（Context 组装）──
// 来源：§8（新建图，guide 中为纯文字描述）
const PROMPT_ASSEMBLY = `flowchart LR
    Q[用户问题] --> S[会话历史摘要<br/>来自 compaction]
    S --> R[近期会话原文<br/>recent N 条]
    R --> M[记忆检索<br/>向量检索相关历史]
    M --> KB[知识库片段<br/>RAG 检索]
    KB --> MERGE[合并 prompt]
    MERGE --> BUDGET{总字符超<br/>context_char_budget?}
    BUDGET -->|是| TRIM[_trim_to_budget<br/>从后向前截断检索片段]
    BUDGET -->|否| OUT[发送给 LLM]
    TRIM --> OUT`;

// ── 图 5：LangGraph 状态机（Workflow loop）──
// 来源：§4
const LANGGRAPH = `stateDiagram-v2
    [*] --> retrieve: PRD 文本
    retrieve --> understand: + sources
    understand --> identify: + understanding
    identify --> generate: + subsystems
    generate --> evaluate: + specs, spec_results
    evaluate --> identify: needs_rework=true 且 rework_passes < max
    evaluate --> [*]: 达标 / 超上限`;

// ── 图 6：Tool calling loop（Workflow loop）──
// 来源：§3
const TOOL_LOOP = `sequenceDiagram
    participant Node as Agent 节点
    participant TL as tool-loop LLMClient
    participant LLM as 大模型
    participant Exec as 本地 tool 执行器
    Node->>TL: system + user + tools(schema)
    loop 直到 LLM 不再要工具 或 达到 max_turns
        TL->>LLM: 当前对话 + 可用工具
        LLM-->>TL: tool_call 或 最终文本
        alt 返回 tool_call
            TL->>Exec: 调对应 handler
            Exec-->>TL: 执行结果
            TL->>TL: 结果作为 tool_result 回填
        else 返回文本
            TL-->>Node: 最终答案 退出循环
        end
    end`;

function DiagramSection({
  title,
  description,
  chart,
}: {
  title: string;
  description: string;
  chart: string;
}) {
  return (
    <div className="mb-8">
      <h3 className="mb-1 text-sm font-semibold text-zinc-900">{title}</h3>
      <p className="mb-3 text-xs leading-5 text-zinc-500">{description}</p>
      <div className="rounded-xl border border-zinc-200 bg-white p-4">
        <MermaidDiagram chart={chart} />
      </div>
    </div>
  );
}

export function ArchitectureView() {
  return (
    <div className="max-w-3xl">
      {/* 系统定位 */}
      <div className="mb-6 rounded-lg border border-sky-200 bg-sky-50 p-4">
        <p className="text-sm font-medium text-sky-900">系统定位</p>
        <p className="mt-1 text-xs leading-5 text-sky-700">
          Porto 是固定 workflow 骨架 + 节点内 agentic 的混合架构。路径预先编排好
          （retrieve → understand → identify → generate → evaluate），每个节点内部
          LLM 用 tool-calling loop 自主取数、用 evaluator-optimizer loop 自我精修。
        </p>
      </div>

      {/* 问话方式 */}
      <h2 className="mb-4 text-base font-semibold">问话方式</h2>
      <DiagramSection
        title="Intent Router：direct vs RAG"
        description="聊天入口先用 LLM（或规则降级）判断这句话走直接回答还是 RAG 检索。LLM 不可用时退回正则 + 关键词匹配。"
        chart={INTENT_ROUTER}
      />
      <DiagramSection
        title="Streaming：聊天 token 流"
        description="prep 阶段（意图/检索/记忆/拼 prompt）非流式一次性完成，答案生成阶段原生 token 流式返回。LLM 不可用时退回 complete 假流式。"
        chart={STREAMING}
      />

      {/* Context 组装 */}
      <h2 className="mb-4 text-base font-semibold">Context 组装</h2>
      <DiagramSection
        title="Memory 三层存储 + Compaction"
        description="原文（SQLite）、向量（ChromaDB）、摘要缓存（SQLite）。会话超阈值时旧消息 LLM 摘要压缩，拼 prompt 用「摘要 + 近期原文」。"
        chart={MEMORY}
      />
      <DiagramSection
        title="Prompt 拼装 + 预算截断"
        description="按固定顺序叠加：用户问题 → 会话摘要 → 近期原文 → 记忆检索 → RAG 片段。总字符超 context_char_budget 时从后向前截断检索片段。"
        chart={PROMPT_ASSEMBLY}
      />

      {/* Workflow loop */}
      <h2 className="mb-4 text-base font-semibold">Workflow Loop</h2>
      <DiagramSection
        title="LangGraph 状态机"
        description="5 个节点串成状态图，state 在节点间流转。evaluate 算出 needs_rework 时条件回边到 identify，rework_passes 计数器防无限回边。"
        chart={LANGGRAPH}
      />
      <DiagramSection
        title="Tool Calling Loop（节点内 agentic）"
        description="每个节点内部 LLM 反复选工具 → 执行 → 回填 → 再思考，直到不再要工具或达到 max_turns。这是 function calling 的工程化实现。"
        chart={TOOL_LOOP}
      />
    </div>
  );
}
```

- [ ] **Step 2: 验证编译通过**

```bash
cd frontend && npm run build
```

Expected: build 成功。

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/architecture-view.tsx
git commit -m "feat(frontend): add ArchitectureView with 6 mermaid diagrams

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 接入 SettingsPage

**Files:**
- Modify: `src/components/porto-workbench.tsx`
  - Line 82: `SettingsSection` type — 加 `"architecture"`
  - Line 1158-1184: sidebar nav 数组 — 加架构按钮
  - Line 1230+: section 渲染区 — 加 architecture 分支
  - Line 3 (imports): 加 `Network` icon from lucide-react

**Interfaces:**
- Consumes: `ArchitectureView` from Task 2
- Consumes: `MermaidDiagram` (transitively, via ArchitectureView)

- [ ] **Step 1: 修改 SettingsSection type**

在 `src/components/porto-workbench.tsx` line 82，把：

```tsx
type SettingsSection = "rag" | "agent" | "document" | "knowledge";
```

改为：

```tsx
type SettingsSection = "rag" | "agent" | "document" | "knowledge" | "architecture";
```

- [ ] **Step 2: 添加 lucide-react Network icon import**

在 line 14-33 的 lucide-react import 块中，加入 `Network`（按字母序插入 `History` 之后、`Loader2` 之前）：

```tsx
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
  ...
} from "lucide-react";
```

- [ ] **Step 3: 添加 ArchitectureView import**

在文件顶部的 import 区域（`SessionList` / `WorkflowList` import 附近，约 line 70-71），加：

```tsx
import { ArchitectureView } from "@/components/architecture-view";
```

- [ ] **Step 4: 在 sidebar nav 数组中添加架构按钮**

在 `SettingsPage` 组件内（约 line 1158-1184），sidebar nav items 数组末尾追加：

```tsx
{
  id: "architecture" as const,
  label: "架构",
  icon: <Network size={15} />,
},
```

完整的数组变为：

```tsx
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
  // ... 现有 button 渲染逻辑不变
))}
```

- [ ] **Step 5: 在 section 渲染区添加 architecture 分支**

在 `SettingsPage` 的 section 内容渲染区（约 line 1230-1231，`knowledge` section 的 `) : null}` 之后），追加：

```tsx
{section === "architecture" ? <ArchitectureView /> : null}
```

- [ ] **Step 6: 验证编译通过**

```bash
cd frontend && npm run build
```

Expected: build 成功，无错误。

- [ ] **Step 7: 手动验证（完整流程）**

```bash
cd frontend && npm run dev
```

打开浏览器访问 frontend URL，操作：
1. 点击左侧 sidebar 的「Settings」按钮
2. 在 Settings 页面左侧看到「架构」tab
3. 点击「架构」tab
4. 看到 6 张 mermaid 图分三组渲染（问话方式 / Context 组装 / Workflow Loop）
5. 每张图都能正常渲染为 SVG（不是原始文本）
6. 图表横向溢出时可滚动

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/porto-workbench.tsx
git commit -m "feat(frontend): wire ArchitectureView into Settings page

Co-Authored-By: Claude <noreply@anthropic.com>"
```
