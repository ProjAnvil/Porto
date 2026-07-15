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
