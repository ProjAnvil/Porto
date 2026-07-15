# 统一 MDXEditor 编辑与预览

**日期**：2026-07-15
**状态**：设计已确认，待实现
**涉及**：前端 `frontend/`

## 1. 背景与目标

上一功能为 spec 卡片引入了 `@mdxeditor/editor`，但遗留三个问题：

1. **spec 编辑态不能滚动**：`max-h-[60vh] overflow-y-auto` 加在 MDXEditor 根 `.mdxeditor` 上，但内部 contentEditable 区有自己的布局，外层 overflow 未在内容区触发，长内容无法滚动。
2. **预览不统一**：spec 卡片预览态、understand/generate 的 `MarkdownCheckpoint` 编辑/预览态仍用 `ReactMarkdown` / `<textarea>`，与编辑态的 MDXEditor 体验割裂。
3. **understand 阶段未用富文本**：用户希望「理解」阶段的编辑与预览也用 MDXEditor。

本次目标：用一个增强的 `SpecMdxEditor` 组件统一四处（spec 编辑/预览、understand/generate 编辑/预览），并彻底修复滚动。

## 2. 决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| MarkdownCheckpoint 编辑/预览切换 | **保留** | 编辑=可编辑+工具栏；预览=只读无工具栏，满足无干扰阅读 |
| generate 是否一并改 | **是** | 与 understand 共用 `MarkdownCheckpoint`，统一一致；generate 草稿（`## 名` 分段）作为长 markdown 编辑，`parseSpecsDraft` 切分逻辑不变 |
| 滚动实现 | **CSS targeting**（工具栏固定 + 内容区滚动） | 不依赖外层 overflow，对 flex 撑满与 max-h 限高两种场景都稳定 |
| 预览渲染 | MDXEditor `readOnly` 替代 ReactMarkdown | 用户要求统一；排版用 `contentEditableClassName="prose ..."` 保持一致 |

## 3. SpecMdxEditor 增强

[spec-mdx-editor.tsx](../../../frontend/src/components/spec-mdx-editor.tsx) 新增两个 prop，并加 wrapper：

```tsx
export function SpecMdxEditor({
  value, onChange, readOnly = false, className,
}: {
  value: string;
  onChange: (markdown: string) => void;
  readOnly?: boolean;
  className?: string;  // 透传给 wrapper，控制高度
}) {
  const plugins = readOnly
    ? [headingsPlugin(), listsPlugin(), quotePlugin(), thematicBreakPlugin(),
       linkPlugin(), codeBlockPlugin(), markdownShortcutPlugin()]  // 无 toolbar
    : [...同上..., toolbarPlugin({ toolbarContents: () => (...) })];
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

要点：
- `readOnly=true` → 不挂 `toolbarPlugin`（预览无工具栏），MDXEditor 传 `readOnly`。
- `contentEditableClassName` 带 prose 样式，编辑/预览排版一致。
- wrapper 恒带 `mdx-editor-scroll`（CSS 定位钩子），`className` 控制尺寸。

## 4. 滚动 CSS（globals.css 追加）

```css
.mdx-editor-scroll { display: flex; flex-direction: column; min-height: 0; }
.mdx-editor-scroll .mdxeditor { flex: 1 1 0; min-height: 0; display: flex; flex-direction: column; }
.mdx-editor-scroll .mdxeditor-root-contenteditable { flex: 1 1 0; min-height: 0; overflow-y: auto; }
```

机制：wrapper（`.mdx-editor-scroll`）是 flex-col，`.mdxeditor` 与内容区都 `flex:1 1 0; min-height:0`，工具栏自然高度固定在顶，内容区 `overflow-y:auto` 滚动。两种尺寸都由 wrapper 承担：
- spec 卡片：wrapper `max-h-[60vh]`（限高，超出则内容区滚）
- MarkdownCheckpoint：wrapper `flex-1 min-h-[280px]`（在 flex-col 父级撑满剩余空间）

`min-height:0` 是关键，避免 flex 子项被 contentEditable 内容撑高导致滚动失效。

## 5. 四处替换

| 位置 | 现在 | 改为 |
|---|---|---|
| SpecCard 编辑态 | `<SpecMdxEditor value onChange />`（className 在 MDXEditor 上，滚动失效） | `<SpecMdxEditor value onChange className="max-h-[60vh]" />` |
| SpecCard 预览态 | `<div prose max-h-60vh overflow><ReactMarkdown/></div>` | `<SpecMdxEditor value readOnly className="max-h-[60vh]" />` |
| MarkdownCheckpoint 编辑态 | `<textarea min-h-280px flex-1>` | `<SpecMdxEditor value onChange className="min-h-[280px] flex-1" />` |
| MarkdownCheckpoint 预览态 | `<div prose flex-1 overflow><ReactMarkdown/></div>` | `<SpecMdxEditor value readOnly className="flex-1" />` |

SpecCard 的 summary 按钮组（下载/编辑/保存/取消）不变；MarkdownCheckpoint 的「编辑/预览」toggle 与底部「保存修改/继续下一步」按钮不变。

## 6. 边界与注意

- **readOnly 预览的代码块**：MDXEditor 用 `codeBlockPlugin` 渲染，不再走 `rehypeHighlight`；视觉与编辑态一致。
- **空内容**：MarkdownCheckpoint 预览态原用 `draft || "_（空）_"`；改 MDXEditor 后空 markdown 渲染为空白，可接受（保留「保存修改」按钮 `disabled={!draft.trim()}` 不变）。
- **generate 草稿切分**：`parseSpecsDraft(draft)` 逻辑不变，MDXEditor 产出仍是含 `## 名` + `\n\n---\n\n` 的合并 markdown。
- **SpecCard 预览态下载**：下载按钮在 summary 行，不受预览组件替换影响，仍下载原始 `body`。
- **动态加载**：`SpecMdxEditor` 仍由 `next/dynamic ssr:false` 加载（已在 porto-workbench 顶部配置），readOnly 预览态同样走该动态组件。

## 7. 不做的事（YAGNI）

- 不改 identify 阶段的 `SubsystemCheckpoint`（它是结构化表单，非 markdown）。
- 不引入 diff/source 插件（无版本对比需求）。
- 不改 spec 卡片的 `<details>` 折叠结构与按钮布局。
- 不动后端（本特性纯前端）。

## 8. 验证方式

- `npx tsc --noEmit` 与 `npx eslint` 通过。
- `npm run build` 成功，SSR 正常。
- 手动 e2e：
  1. spec 卡片编辑态：长内容可在卡片内滚动，工具栏固定。
  2. spec 卡片预览态：渲染为 MDXEditor 只读，与编辑态排版一致；内容长可滚。
  3. understand checkpoint：编辑态 MDXEditor 可编辑、可滚；预览态只读渲染；保存/继续正常。
  4. generate checkpoint：同上，保存后 `parseSpecsDraft` 仍正确切分多 spec。

## 9. 改动清单

- `frontend/src/components/spec-mdx-editor.tsx`：加 `readOnly`/`className` prop + wrapper + 条件 toolbar。
- `frontend/src/app/globals.css`：追加 `.mdx-editor-scroll` 滚动规则。
- `frontend/src/components/porto-workbench.tsx`：
  - `SpecCard` 编辑态 wrapper 改用 `className` 传高度；预览态 `ReactMarkdown` → `SpecMdxEditor readOnly`。
  - `MarkdownCheckpoint` 编辑态 `<textarea>` → `SpecMdxEditor`；预览态 `ReactMarkdown` → `SpecMdxEditor readOnly`。
