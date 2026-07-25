"use client";

import MDEditor from "@uiw/react-md-editor";
import "@uiw/react-md-editor/markdown-editor.css";

/**
 * Markdown 编辑器(@uiw/react-md-editor,textarea + 预览)。
 *
 * 替代旧 MDXEditor(WYSIWYG):后者解析含 box-drawing 字符(├── └── ← →)的 fenced
 * code block 时会丢掉代码块及之后的所有内容(textarea 源码存储从不丢)。预览走 GFM,
 * 代码块/表格/特殊字符可靠渲染。
 *
 * preview:
 *   "live"    分栏(编辑 | 预览),默认
 *   "edit"    纯编辑
 *   "preview" 只预览(隐藏工具栏)
 */
export function SpecMdxEditor({
  value,
  onChange,
  preview = "live",
  className,
  height = "100%",
}: {
  value: string;
  onChange?: (markdown: string) => void;
  preview?: "live" | "edit" | "preview";
  className?: string;
  height?: number | string;
}) {
  return (
    <div className={className} data-color-mode="light">
      <MDEditor
        value={value}
        onChange={(val) => onChange?.(val ?? "")}
        preview={preview}
        hideToolbar={preview === "preview"}
        height={height}
      />
    </div>
  );
}
