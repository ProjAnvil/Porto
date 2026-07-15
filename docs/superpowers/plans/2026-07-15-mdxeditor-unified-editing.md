# 统一 MDXEditor 编辑与预览 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 用增强的 `SpecMdxEditor` 统一 spec 卡片与 understand/generate checkpoint 的编辑/预览，并修复 MDXEditor 滚动。

**Architecture:** `SpecMdxEditor` 加 `readOnly`/`className` + wrapper；globals.css 用 CSS targeting 让工具栏固定、contentEditable 区滚动；porto-workbench 四处替换 textarea/ReactMarkdown。

**Tech Stack:** Next.js 16 / React 19 / Tailwind v4 / `@mdxeditor/editor@4`。

## Global Constraints

- **前端无单元测试框架**：验证用 `npx tsc --noEmit` + `npx eslint <file>` + `npm run build` + 手动 e2e。
- **不动后端**，纯前端。
- **保留**：ReactMarkdown / rehypeHighlight / remarkGfm 的 import（CompletedView 的 understand tab 仍用）。
- **保留**：SpecCard summary 按钮组、MarkdownCheckpoint 的「编辑/预览」toggle 与底部按钮、`<details>` 折叠。
- **MDXEditor 滚动**：靠 `.mdx-editor-scroll` CSS（flex + min-height:0 + contentEditable overflow），不靠外层 overflow。
- **commit** 消息末尾加 `Co-Authored-By: Claude <noreply@anthropic.com>`，直接在 main 提交。

---

## Task 1: SpecMdxEditor 增强（readOnly + className + wrapper）

**Files:**
- Modify: `frontend/src/components/spec-mdx-editor.tsx`（整体重写组件体）

**Interfaces:**
- Produces: `SpecMdxEditor({ value, onChange, readOnly?, className? })`。`readOnly=true` 时不挂 toolbarPlugin；wrapper 恒带 `mdx-editor-scroll`，`className` 透传给 wrapper 控制高度。

- [ ] **Step 1: 重写 spec-mdx-editor.tsx**

整体替换 `frontend/src/components/spec-mdx-editor.tsx` 内容为：

```tsx
"use client";

import "@mdxeditor/editor/style.css";
import {
  BlockTypeSelect,
  BoldItalicUnderlineToggles,
  CreateLink,
  ListsToggle,
  UndoRedo,
  codeBlockPlugin,
  headingsPlugin,
  linkPlugin,
  listsPlugin,
  markdownShortcutPlugin,
  quotePlugin,
  thematicBreakPlugin,
  toolbarPlugin,
  MDXEditor,
} from "@mdxeditor/editor";

export function SpecMdxEditor({
  value,
  onChange,
  readOnly = false,
  className,
}: {
  value: string;
  onChange: (markdown: string) => void;
  readOnly?: boolean;
  className?: string;
}) {
  const basePlugins = [
    headingsPlugin(),
    listsPlugin(),
    quotePlugin(),
    thematicBreakPlugin(),
    linkPlugin(),
    codeBlockPlugin(),
    markdownShortcutPlugin(),
  ];
  const plugins = readOnly
    ? basePlugins
    : [
        ...basePlugins,
        toolbarPlugin({
          toolbarContents: () => (
            <>
              <UndoRedo />
              <BlockTypeSelect />
              <BoldItalicUnderlineToggles />
              <ListsToggle />
              <CreateLink />
            </>
          ),
        }),
      ];
  return (
    <div className={`mdx-editor-scroll ${className ?? ""}`}>
      <MDXEditor
        readOnly={readOnly}
        markdown={value}
        onChange={onChange}
        contentEditableClassName="prose prose-zinc max-w-none prose-pre:rounded-lg prose-pre:bg-zinc-950 prose-pre:text-zinc-50"
        plugins={plugins}
      />
    </div>
  );
}
```

- [ ] **Step 2: 类型检查 + lint**

Run: `cd frontend && npx tsc --noEmit && npx eslint src/components/spec-mdx-editor.tsx`
Expected: 无错误。（若 `plugins` 数组类型报错，给 `basePlugins` 显式注解或用 `as` 断言适配 MDXEditor 的 plugins 类型。）

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/spec-mdx-editor.tsx
git commit -m "$(cat <<'EOF'
refactor(frontend): SpecMdxEditor 支持 readOnly + className

新增 readOnly（无 toolbar 只读渲染）与 className（透传 wrapper 控高），
为统一预览与滚动修复铺路。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: globals.css 加 MDXEditor 滚动规则

**Files:**
- Modify: `frontend/src/app/globals.css`（末尾追加）

- [ ] **Step 1: 追加滚动 CSS**

在 `frontend/src/app/globals.css` 末尾追加：

```css

.mdx-editor-scroll {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.mdx-editor-scroll .mdxeditor {
  flex: 1 1 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.mdx-editor-scroll .mdxeditor-root-contenteditable {
  flex: 1 1 0;
  min-height: 0;
  overflow-y: auto;
}
```

- [ ] **Step 2: 提交**

```bash
git add frontend/src/app/globals.css
git commit -m "$(cat <<'EOF'
style(frontend): MDXEditor 滚动 CSS（工具栏固定 + 内容区滚动）

.mdx-editor-scroll 用 flex + min-height:0 让 contentEditable 区滚动，
适配 max-h 限高与 flex-1 撑满两种场景。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: porto-workbench 四处替换

**Files:**
- Modify: `frontend/src/components/porto-workbench.tsx`：
  - `SpecCard` 编辑态（加 className）+ 预览态（ReactMarkdown → SpecMdxEditor readOnly）
  - `MarkdownCheckpoint` 编辑态（textarea → SpecMdxEditor）+ 预览态（ReactMarkdown → SpecMdxEditor readOnly）

**Interfaces:**
- Consumes: `SpecMdxEditor`（Task 1）。

- [ ] **Step 1: SpecCard 编辑/预览态**

把 SpecCard 内的编辑/预览分支：

```tsx
      {editing ? (
        <div className="px-3 pb-3">
          <SpecMdxEditor value={draft} onChange={setDraft} />
        </div>
      ) : (
        <div className="prose prose-zinc max-h-[60vh] max-w-none overflow-y-auto px-3 pb-3 prose-pre:rounded-lg prose-pre:bg-zinc-950 prose-pre:text-zinc-50">
          <ReactMarkdown
            rehypePlugins={[rehypeHighlight]}
            remarkPlugins={[remarkGfm]}
          >
            {body}
          </ReactMarkdown>
        </div>
      )}
```

替换为：

```tsx
      {editing ? (
        <div className="px-3 pb-3">
          <SpecMdxEditor
            value={draft}
            onChange={setDraft}
            className="max-h-[60vh]"
          />
        </div>
      ) : (
        <div className="px-3 pb-3">
          <SpecMdxEditor value={body} readOnly className="max-h-[60vh]" />
        </div>
      )}
```

- [ ] **Step 2: MarkdownCheckpoint 编辑/预览态**

把 `MarkdownCheckpoint` 内的预览/编辑分支：

```tsx
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
```

替换为：

```tsx
      {preview ? (
        <SpecMdxEditor value={draft} readOnly className="flex-1 p-4" />
      ) : (
        <SpecMdxEditor
          value={draft}
          onChange={setDraft}
          className="min-h-[280px] flex-1 p-4"
        />
      )}
```

- [ ] **Step 3: 类型检查 + lint**

Run: `cd frontend && npx tsc --noEmit && npx eslint src/components/porto-workbench.tsx`
Expected: 无新增错误（既有 `react-hooks/set-state-in-effect`、`react-hooks/exhaustive-deps` 与本次无关）。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/components/porto-workbench.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): spec 卡片与 checkpoint 预览统一用 MDXEditor

SpecCard 预览态、MarkdownCheckpoint 编辑/预览态改用 SpecMdxEditor
（编辑=可编辑+toolbar，预览=readOnly），textarea 与 ReactMarkdown
退出这两个位置；滚动靠 .mdx-editor-scroll CSS。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 验证

**Files:** 无（仅验证）。

- [ ] **Step 1: 类型 + lint 全量**

Run: `cd frontend && npx tsc --noEmit && npx eslint src/`
Expected: 无新增错误。

- [ ] **Step 2: 构建**

Run: `cd frontend && npm run build 2>&1 | tail -30`
Expected: 构建成功，无 SSR 报错。

- [ ] **Step 3: 手动 e2e（用户侧）**

`cd frontend && npm run dev`，浏览器验证：
1. completed 工作流→规格 tab→展开某 spec→「编辑」：富文本编辑器，**长内容可在卡片内滚动，工具栏固定**；保存后内容刷新。
2. spec 预览态（非编辑）：MDXEditor 只读渲染，与编辑态排版一致；长内容可滚。
3. awaiting_input 的 understand 工作流→「编辑」：MDXEditor 可编辑、可滚；「预览」：只读渲染；「保存修改」/「继续下一步」正常。
4. generate checkpoint：同 3，保存后 specs 仍正确切分（展开各 spec 卡片可见）。

Expected: 全部通过。（此步需用户在浏览器确认。）

- [ ] **Step 4: 如有问题修复后追加提交；无则结束。**
