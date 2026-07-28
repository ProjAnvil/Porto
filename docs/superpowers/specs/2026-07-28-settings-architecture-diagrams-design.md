# Settings 页面新增「架构图」面板

> 日期：2026-07-28
> 状态：已批准，待实现

## 目标

在 Porto 前端 Settings 页面新增一个「架构」tab，用 mermaid 流程图展示 agent 的三大运作机制，让用户在不读后端代码的情况下直观理解系统如何工作。

三个展示领域：
1. **问话方式** — 用户消息进来后如何被分类、如何流式回答
2. **Context 组装** — memory 三层存储 + prompt 拼装策略
3. **Workflow loop** — LangGraph 状态机 + 节点内 tool calling 循环

## 设计

### 组件结构

```
PortoWorkbench (现有)
  └─ SettingsPage (现有)
       └─ sidebar: rag | agent | document | knowledge | 架构 ← 新增
       └─ section === "architecture"
            └─ ArchitectureView (新组件)
                 ├─ 系统定位介绍（固定 workflow 骨架 + 节点内 agentic）
                 ├─ MermaidDiagram × 6（按三个领域分组，每组配中文说明）
```

### 新增文件

1. **`src/components/mermaid-diagram.tsx`** — 可复用 mermaid 渲染组件
   - `"use client"` 声明
   - `dynamic(() => import("mermaid"), { ssr: false })` 动态加载 mermaid 库
   - Props: `{ chart: string; className?: string }`
   - 用 `useEffect` + `useRef` 调 `mermaid.render(id, chart)` 得到 SVG 字符串，注入 ref 容器
   - `mermaid.initialize({ startOnLoad: false, theme: "default" })` — light theme，与 app `data-color-mode="light"` 一致
   - 为每个图生成唯一 id（`useId()` 或递增计数），避免多实例 id 冲突
   - 加载中显示 spinner（复用 `Loader2`）
   - 渲染失败时 fallback 显示原始 chart 文本（`<pre>`）

2. **`src/components/architecture-view.tsx`** — 架构面板内容
   - 三个 section（问话方式 / Context 组装 / Workflow loop），每个含标题 + 说明 + 1-2 张图
   - Diagram 内容硬编码为 TS 常量（静态文档，不依赖运行时 config）

### 修改文件

3. **`src/components/porto-workbench.tsx`** — 接入新面板
   - `SettingsSection` type 加 `"architecture"`
   - `SettingsPage` sidebar nav 数组加 `{ id: "architecture", label: "架构", icon: <Network size={15} /> }`
   - `section === "architecture"` 时渲染 `<ArchitectureView />`
   - `lucide-react` import 加 `Network` icon

### 内容：6 张图

所有 mermaid chart 源自 `docs/backend-agent-guide.md` 的现有图（§8 除外，需新建）。

| 分组 | 图标题 | guide 来源 | mermaid 类型 |
|------|--------|-----------|-------------|
| 问话方式 | Intent router：direct vs RAG 判定 | §6 | flowchart LR |
| 问话方式 | Streaming：聊天 token 流 | §9 | sequenceDiagram |
| Context 组装 | Memory 三层存储 + compaction | §7 | flowchart TB |
| Context 组装 | Prompt 拼装顺序 + 预算截断 | §8（新建） | flowchart LR |
| Workflow loop | LangGraph 状态机（5 节点 + 回边） | §4 | stateDiagram-v2 |
| Workflow loop | Tool calling loop（节点内 agentic 循环） | §3 | sequenceDiagram |

#### §8 新建图：Prompt 拼装

```
flowchart LR
    Q[用户问题] --> S[会话历史摘要<br/>来自 compaction]
    S --> R[近期会话原文<br/>recent N 条]
    R --> M[记忆检索<br/>向量检索相关历史]
    M --> KB[知识库片段<br/>RAG 检索]
    KB --> MERGE[合并 prompt]
    MERGE --> BUDGET{总字符超<br/>context_char_budget?}
    BUDGET -->|是| TRIM[_trim_to_budget<br/>从后向前截断检索片段]
    BUDGET -->|否| OUT[发送给 LLM]
    TRIM --> OUT
```

### MermaidDiagram 渲染策略

```tsx
// 伪代码
const MermaidLib = dynamic(() => import("mermaid"), { ssr: false });

function MermaidDiagram({ chart }: { chart: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId().replace(/:/g, ""); // mermaid id 不能有冒号

  useEffect(() => {
    if (!MermaidLib) return;
    mermaid.initialize({ startOnLoad: false, theme: "default" });
    const { svg } = await mermaid.render(`mmd-${id}`, chart);
    if (ref.current) ref.current.innerHTML = svg;
  }, [chart]);

  return <div ref={ref} className="overflow-x-auto" />;
}
```

实际实现时需查阅 `node_modules/next/dist/docs/` 确认 Next.js 16 canary 的 `dynamic` / `useId` API（项目 AGENTS.md 要求）。

## 不做的事

- **不做 config-aware 动态图** — 图是静态文档，不反映实时设置值（如 max_turns）。这是后续 followup。
- **不单独展示 Spec evaluator-optimizer（§5）** — 它嵌在 generate 节点内部，tool calling loop 图已覆盖节点级循环。如需可后续追加。
- **不加暗色主题** — app 当前是 `data-color-mode="light"` 单主题，mermaid `theme: "default"` 即可。
- **不改后端** — 纯前端改动，无 API 变更。

## 依赖

- 新增 npm 包：`mermaid`（官方包，~client-side SVG 渲染）
- 无后端变更
