# Spec 卡片编辑与下载

**日期**：2026-07-15
**状态**：设计已确认，待实现
**涉及**：前端 `frontend/`、后端 `backend/src/porto_chatbot/`

## 1. 背景与目标

Porto 工作流完成后，`CompletedView` 的「规格」tab 会按子系统渲染每个 spec 卡片（`<details>`，内容为 markdown）。当前这些 spec 只能看不能改。

本设计为每个 spec 卡片新增**编辑**与**下载**能力：

- 编辑：点击后在卡片内原位渲染 MDX 富文本编辑器（`@mdxeditor/editor`），编辑后可保存。
- 下载：每个 spec 单独下载为 `{name}.md`；specs tab 顶部提供「下载全部」，合并为单个 markdown。
- 保存：把改动写回 `generate.output.specs`，**不触发下游重算、不改变工作流状态**。

## 2. 关键约束（探索发现）

- **specs 来源**：`detail.outputs.generate.output.specs`，类型 `Record<string, string>`（key=子系统名，value=markdown 正文）。
- **现有保存接口有破坏性副作用**：`PUT /api/porto/workflows/{id}/steps/{step}`（[workflow.py:228](../../../backend/src/porto_chatbot/api/routes/workflow.py)）对 `generate` 步骤会：
  1. 覆盖整步 output；
  2. `clear_outputs_after` —— 清空 `evaluate` 等下游产出；
  3. 状态退回 `awaiting_input`。
  而 `CompletedView` 仅在 `status === "completed"` 时渲染（[porto-workbench.tsx:1994](../../../frontend/src/components/porto-workbench.tsx)）。直接复用会导致保存后视图消失，不可接受。
- **`generate` 在可编辑白名单** `_EDITABLE_STEPS = {"understand","identify","generate"}`，但白名单配套的是"回退重跑"语义，与本次"轻量改字"诉求不符 → 需另设接口。
- **依赖可行性**：`@mdxeditor/editor@4.x` 的 `peerDependencies.react = ">= 18 || >= 19"`，支持当前 React 19.2.4。
- **WorkflowDetail 含 `workflow_id`**，保存接口所需 id 可由 `detail` 直接取得。

## 3. 决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 保存语义 | 新增轻量 `PATCH /specs`，只改 `specs[name]`，不清下游、不改状态 | 用户意图是"改动保存下来的 spec 文件"，不要重跑评分 |
| 编辑器 | `@mdxeditor/editor` 富文本 + Markdown 快捷输入 | 用户指定；提供所见即所得体验 |
| 编辑切换 | 卡片内原位切换（预览 ⇄ 编辑器），非弹窗 | 用户明确"编辑之后就开始渲染这个 markdown editor" |
| 编辑粒度 | 每个 spec 卡片独立编辑态，可同时多个 | 灵活、简单，符合"每个卡片右侧加按钮"的描述 |
| 按钮位置 | summary 行右侧（折叠态可下载，点编辑自动展开） | 用户确认 |
| 下载范围 | 单个 `{name}.md` + tab 顶部「下载全部」 | 用户确认 |
| 折叠能力 | 保留 `<details>` 折叠 | 用户未要求移除，多 spec 时有用 |

## 4. 架构

### 4.1 后端：新增轻量保存接口

```
PATCH /api/porto/workflows/{workflow_id}/specs
  body: { name: string, body: string }
  - workflow 不存在 → 404
  - 无 generate output 或 name 不在 specs 中 → 400
  - 仅更新 generate.output.specs[name] = body
  - 不动 produced_by / produced_at 审计字段，不调用 clear_outputs_after，不改 status / current_step
返回：更新后的 WorkflowDetail
```

实现要点：
- `api/routes/workflow.py` 新增 `@router.patch("/api/porto/workflows/{workflow_id}/specs", ...)` handler。
- `WorkflowStore` 新增方法 `update_spec(workflow_id, name, body)`：读取现有 generate output dict → 改 `specs[name]` → 写回（沿用 store 现有持久化方式）。
- 不复用 `save_output`（它会动审计/触发后续逻辑），单独的窄方法更安全。

### 4.2 前端数据流

```
Workbench
  ├─ onSaveSpec(name, body) → updateWorkflowSpec(workflowId, name, body)
  │      成功：setWorkflowDetail(返回的 detail)；失败：setError，保留编辑态
  └─ WorkflowPanel(onSaveSpec=…)
       └─ CompletedView(detail, onSaveSpec, busy)
            └─ specs tab：顶部「下载全部」+ SpecCard 列表
                 └─ SpecCard(name, body, onSave, busy)  ← 每卡片自管 editing/draft
```

- `api.ts` 新增 `updateWorkflowSpec(id, name, body)` → `PATCH`，返回 `WorkflowDetail`。
- 回调链沿用 `onSaveStep` 的成熟模式（[porto-workbench.tsx:428](../../../frontend/src/components/porto-workbench.tsx)）：调用 API → 用返回 detail 刷新 state → 清 draft。

## 5. 组件设计：SpecCard

从 `CompletedView` 拆出独立组件，每个 spec 一个实例。

**Props**：`name: string`、`body: string`、`onSave(name, body): Promise<void>`、`busy: boolean`。

**State**：`editing: boolean`、`draft: string`。

**结构**（保留 `<details>`）：

```
<details>
  <summary>  ← flex 行
    左：spec 名（保留默认/自定义折叠三角）
    右：按钮组
      预览态：[下载] [编辑]
      编辑态：[保存(loading)] [取消]
  </summary>
  内容区：
    预览态：<div prose max-h-[60vh] overflow-y-auto><ReactMarkdown/></div>
    编辑态：<SpecMdxEditor markdown={draft} onChange={setDraft} />（同样限高滚动）
</details>
```

**交互细节**：
- `<details>` 的 `open` 由组件 state 受控（进入编辑态时强制 `open=true`）。
- 按钮组所有 `onClick` 内 `e.stopPropagation()`，避免触发 `<details>` 折叠。
- 点「编辑」：`setDraft(body)`、`setEditing(true)`；若 details 未展开则同时展开（设 `open`）。
- 点「保存」：调 `onSave(name, draft)`；成功 → `setEditing(false)`；失败 → 保持编辑态与 draft（不丢输入）。
- 点「取消」：`setEditing(false)`、丢弃 draft（恢复用最新 `body`）。
- `busy` 为 true 时：禁用「保存」「取消」「编辑」「下载」。

## 6. MDXEditor 集成

- 新建 `frontend/src/components/spec-mdx-editor.tsx`，封装 `MDXEditor`。
- 用 `next/dynamic(() => import(...), { ssr: false })` 包装：MDXEditor 基于 Lexical、依赖 DOM，必须跳过 SSR（porto-workbench 虽是 client component，但 Next.js 仍可能在 SSR 阶段渲染，dynamic ssr:false 最稳妥）。
- 插件集：`headings`、`lists`、`link`、`quote`、`thematicBreak`、`codeBlock` + `toolbar`（粗体/斜体/标题/列表/引用/链接/代码）+ `markdownShortcutPlugin`（`#`、`-`、`>` 等快捷输入）。
- 受控：`markdown={draft}`、`onChange={setDraft}`。
- 样式：`import "@mdxeditor/editor/style.css"`（在该组件文件顶部，仅 client 侧生效）。
- 高度：编辑器容器限高 `max-h-[60vh] overflow-y-auto`，与预览态一致。

## 7. 下载实现

纯前端，无需后端。

- 单个 spec：`triggerDownload(\`${name}.md\`, body)` —— Blob(`text/markdown`) → `<a download>` 点击。
- 下载全部（specs tab 顶部按钮）：合并所有 spec 为单个 markdown：
  ```
  # {name1}

  {body1}

  ---

  # {name2}

  {body2}
  ```
  文件名：`${project_name || "specs"}.md`（`project_name` 取自 `detail.project_name`）。
- `triggerDownload` 为通用工具函数，放 `frontend/src/lib/download.ts`（或并入现有 util）。

## 8. 边界与错误处理

- 保存中：按钮 loading + 禁用操作，避免并发提交。
- 保存失败：沿用 `setError` 通路展示错误；保留编辑态与 draft。
- 后端校验失败（400）：同上，前端展示错误信息。
- name 含特殊字符：下载文件名做 sanitize（去掉 `/`、`\` 等非法字符）。
- specs 为空：保持现有"无规格输出"占位；「下载全部」按钮在无 specs 时隐藏或禁用。

## 9. 不做的事（YAGNI）

- 不编辑 `understanding` / `subsystems`（只做 specs）。
- 不做富媒体/图片上传。
- 不打包 zip 下载（合并 markdown 已满足"下载全部"）。
- 不做版本历史/diff（保存为覆盖语义）。
- 不改 `_EDITABLE_STEPS` 或现有 `PUT /steps/{step}` 的回退语义。

## 10. 验证方式

- 后端：为 `PATCH /specs` 新增 pytest（成功更新 / 404 / 400 / 下游未清空 / 状态未变）。
- 前端：
  - `tsc --noEmit` 与 `eslint` 通过。
  - 手动 e2e：打开一个 completed 工作流 → 规格tab → 编辑某 spec → 保存 → 内容刷新且仍处于 completed 视图；下载单个 / 下载全部；取消编辑恢复原文；保存失败时报错且 draft 保留。

## 11. 实现改动清单

**后端**
- `backend/src/porto_chatbot/api/routes/workflow.py`：新增 `PATCH .../specs` handler。
- `backend/src/porto_chatbot/`（store 所在）：新增 `update_spec(workflow_id, name, body)`。
- `backend/tests/test_workflow_api.py`：新增用例。

**前端**
- 新增 `frontend/src/components/spec-mdx-editor.tsx`（MDXEditor 封装，dynamic ssr:false）。
- 新增 `frontend/src/lib/download.ts`（`triggerDownload`）。
- `frontend/src/lib/api.ts`：新增 `updateWorkflowSpec`。
- `frontend/src/components/porto-workbench.tsx`：
  - `CompletedView` 接收 `onSaveSpec` / `busy`，拆出 `SpecCard`，specs tab 顶部加「下载全部」。
  - `WorkflowPanel` 透传 `onSaveSpec`。
  - 顶层 `PortoWorkbench`（`onSaveStep` 所在处，[porto-workbench.tsx:428](../../../frontend/src/components/porto-workbench.tsx)）实现 `onSaveSpec` handler。
- `frontend/package.json`：新增依赖 `@mdxeditor/editor`。
