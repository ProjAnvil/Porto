"use client";

import MarkdownPreview from "@uiw/react-markdown-preview";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "@uiw/react-markdown-preview/markdown.css";

/**
 * Markdown 纯渲染(@uiw/react-markdown-preview,基于 react-markdown)。
 *
 * 统一替代散落的 ReactMarkdown 调用。GFM 表格 + 代码块高亮;对 box-drawing 字符
 * (├── └── ← → 【】)的 fenced code block 完整渲染,绝不丢内容。SSR 友好,可直接 import。
 */
export function MarkdownView({
  value,
  className,
}: {
  value: string;
  className?: string;
}) {
  return (
    <div className={className} data-color-mode="light">
      <MarkdownPreview
        source={value}
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
      />
    </div>
  );
}
