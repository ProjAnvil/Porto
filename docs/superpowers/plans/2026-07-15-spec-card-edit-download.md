# Spec 卡片编辑与下载 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 CompletedView 的每个 spec 卡片支持原位富文本编辑（保存到后端）、单个下载、以及 tab 级「下载全部」。

**Architecture:** 后端新增轻量 `PATCH /api/porto/workflows/{id}/specs`（只改 `generate.output.specs[name]`，不动审计/状态/下游），前端用 `@mdxeditor/editor`（dynamic ssr:false）在 SpecCard 内做预览⇄编辑切换，下载走纯前端 Blob。

**Tech Stack:** 后端 Python / FastAPI / sqlite（WorkflowStore）+ pytest；前端 Next.js 16 canary / React 19.2.4 / TypeScript / Tailwind v4；`@mdxeditor/editor@^4.0.4`。

## Global Constraints

- **保存语义**：`PATCH /specs` 仅更新 `generate.output.specs[name]`，**不动** `produced_by`/`produced_at`，**不调** `clear_outputs_after`，**不改** `status`/`current_step`。
- **store 持久化**：`workflow_outputs` 表，`output` 列存 `json.dumps(..., ensure_ascii=False)`；`get_outputs` 已 `json.loads`。
- **store 测试构造**：`WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path/\"logs\"))`；`store.create(session_id, project_name, prd_text, top_k, rag_snapshot, agent_snapshot)`。
- **API 测试造数**：`from porto_chatbot.api.deps import get_workflow_store` 拿到与 TestClient 同一单例（按 data_dir 缓存），直接 `store.create` + `store.save_output` 注入 generate/evaluate，**不经 executor**（确定性、无 LLM、无竞态）。
- **前端无单元测试框架**：前端验证一律用 `npx tsc --noEmit` 与 `npx eslint <file>`；后端用 pytest。
- **MDXEditor SSR**：基于 Lexical 依赖 DOM，必须 `next/dynamic(..., { ssr: false })`；CSS `import \"@mdxeditor/editor/style.css\"`。
- **commit**：每个 Task 末尾提交，消息格式 `type(scope): 描述`，末尾加 `Co-Authored-By: Claude <noreply@anthropic.com>`。直接在 main 提交（项目既有工作流）。

---

## Task 1: 后端 WorkflowStore.update_spec（TDD）

**Files:**
- Modify: `backend/src/porto_chatbot/workflow_store.py`（在 `clear_outputs_after` 与 `update_status` 之间，约 151 行处插入）
- Test: `backend/tests/test_workflow_store.py`（追加两个测试）

**Interfaces:**
- Produces: `WorkflowStore.update_spec(workflow_id: str, name: str, body: str) -> bool`。`True`=找到并更新；`False`=无 generate output、`specs` 非 dict、或 `name` 不在 specs。

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_workflow_store.py` 末尾：

```python
def test_update_spec_updates_only_named_spec(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "p", "prd", 6, {}, {})
    s.save_output(wid, "generate", {"specs": {"Auth": "原", "Pay": "原Pay"}}, "ai")
    s.save_output(wid, "evaluate", {"score": 8}, "ai")

    ok = s.update_spec(wid, "Auth", "新正文")
    assert ok is True
    outs = s.get_outputs(wid)
    assert outs["generate"]["output"]["specs"]["Auth"] == "新正文"
    assert outs["generate"]["output"]["specs"]["Pay"] == "原Pay"  # 其他 spec 不变
    assert outs["generate"]["produced_by"] == "ai"  # 审计字段不变
    assert outs["evaluate"]["output"]["score"] == 8  # 下游不变
    assert "evaluate" in outs


def test_update_spec_missing_returns_false(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "p", "prd", 6, {}, {})
    assert s.update_spec(wid, "Auth", "x") is False  # 无 generate output
    s.save_output(wid, "generate", {"specs": {"Auth": "原"}}, "ai")
    assert s.update_spec(wid, "Nope", "x") is False  # name 不在 specs
    s.save_output(wid, "generate", {"specs": "not a dict"}, "ai")
    assert s.update_spec(wid, "Auth", "x") is False  # specs 非 dict
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_workflow_store.py::test_update_spec_updates_only_named_spec tests/test_workflow_store.py::test_update_spec_missing_returns_false -v`
Expected: FAIL，`AttributeError: 'WorkflowStore' object has no attribute 'update_spec'`。

- [ ] **Step 3: 实现 update_spec**

在 `backend/src/porto_chatbot/workflow_store.py` 的 `clear_outputs_after` 方法之后、`update_status` 方法之前插入：

```python
    def update_spec(self, workflow_id, name, body) -> bool:
        """轻量更新 generate 步 output 中的 specs[name]。

        不动 produced_by/produced_at、不清下游、不改 status/current_step。
        返回 True 表示找到并更新；False 表示无 generate output、specs 非 dict
        或 name 不在 specs 中。
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT output FROM workflow_outputs"
                " WHERE workflow_id=? AND step_name='generate'",
                (workflow_id,),
            ).fetchone()
            if row is None:
                return False
            output = json.loads(row["output"])
            specs = output.get("specs")
            if not isinstance(specs, dict) or name not in specs:
                return False
            specs[name] = body
            conn.execute(
                "UPDATE workflow_outputs SET output=?"
                " WHERE workflow_id=? AND step_name='generate'",
                (json.dumps(output, ensure_ascii=False), workflow_id),
            )
        return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_workflow_store.py -v`
Expected: PASS（含新增两个 + 既有全部）。

- [ ] **Step 5: 提交**

```bash
git add backend/src/porto_chatbot/workflow_store.py backend/tests/test_workflow_store.py
git commit -m "$(cat <<'EOF'
feat(backend): WorkflowStore.update_spec 轻量更新单个 spec

只改 generate.output.specs[name]，不动审计/状态/下游。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 后端 PATCH /specs 路由 + API 测试（TDD）

**Files:**
- Modify: `backend/src/porto_chatbot/api/routes/workflow.py`（新增 `SpecUpdateRequest` model + PATCH handler，插在 `delete_workflow` 之前）
- Test: `backend/tests/test_workflow_api.py`（追加一个测试）

**Interfaces:**
- Consumes: `WorkflowStore.update_spec`（Task 1 产出）
- Produces: `PATCH /api/porto/workflows/{workflow_id}/specs`，body `{name, body}`，返回 `WorkflowDetail`；404=workflow 不存在，400=spec 不存在。

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_workflow_api.py` 末尾：

```python
def test_patch_spec_updates_without_side_effects(monkeypatch, sample_settings):
    """PATCH /specs：只改 generate.specs[name]，不改 status/current_step、
    不清下游、不动 produced_by。name 不存在→400；workflow 不存在→404。"""
    from porto_chatbot.api.deps import get_workflow_store

    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        store = get_workflow_store()
        wid = store.create("s1", "proj", "prd", 6, {}, {})
        store.save_output(wid, "generate", {"specs": {"Auth": "原始"}}, "ai")
        store.save_output(wid, "evaluate", {"score": 10}, "ai")
        store.update_status(wid, "completed", current_step="evaluate")

        resp = client.patch(
            f"/api/porto/workflows/{wid}/specs",
            json={"name": "Auth", "body": "编辑后正文"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["outputs"]["generate"]["output"]["specs"]["Auth"] == "编辑后正文"
        # 副作用均未发生
        assert body["status"] == "completed"
        assert body["current_step"] == "evaluate"
        assert "evaluate" in body["outputs"]
        assert body["outputs"]["evaluate"]["output"]["score"] == 10
        assert body["outputs"]["generate"]["produced_by"] == "ai"

        # name 不存在 → 400
        bad = client.patch(
            f"/api/porto/workflows/{wid}/specs",
            json={"name": "Nope", "body": "x"},
        )
        assert bad.status_code == 400

        # workflow 不存在 → 404
        miss = client.patch(
            "/api/porto/workflows/missing/specs",
            json={"name": "Auth", "body": "x"},
        )
        assert miss.status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_workflow_api.py::test_patch_spec_updates_without_side_effects -v`
Expected: FAIL，404/405 或路由不存在相关错误。

- [ ] **Step 3: 实现 SpecUpdateRequest model**

在 `backend/src/porto_chatbot/api/routes/workflow.py` 的 `WorkflowDetail` 类定义之后（约 98 行后）插入：

```python
class SpecUpdateRequest(BaseModel):
    name: str
    body: str
```

- [ ] **Step 4: 实现 PATCH handler**

在 `save_step_output`（PUT /steps）handler 之后、`delete_workflow` 之前插入：

```python
@router.patch(
    "/api/porto/workflows/{workflow_id}/specs", response_model=WorkflowDetail
)
def update_spec(workflow_id: str, payload: SpecUpdateRequest):
    """轻量更新某个 spec 正文：只改 generate.output.specs[name]，
    不动审计字段、不清下游、不改 status/current_step。
    workflow 不存在→404；无 generate output 或 name 不在 specs→400。"""
    store = get_workflow_store()
    if store.get(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    if not store.update_spec(workflow_id, payload.name, payload.body):
        raise HTTPException(400, "spec not found")
    return _detail(store, workflow_id)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_workflow_api.py::test_patch_spec_updates_without_side_effects -v`
Expected: PASS。

- [ ] **Step 6: 跑完整后端测试，确认无回归**

Run: `cd backend && python -m pytest tests/ -q 2>&1 | tail -20`
Expected: 无新增失败（既有可能存在与本功能无关的环境相关 skip/fail，关注新增测试通过且无回归）。

- [ ] **Step 7: 提交**

```bash
git add backend/src/porto_chatbot/api/routes/workflow.py backend/tests/test_workflow_api.py
git commit -m "$(cat <<'EOF'
feat(backend): PATCH /api/porto/workflows/{id}/specs 轻量保存

仅更新 generate.output.specs[name]，不清下游、不改状态；
workflow 不存在 404，spec 不存在 400。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 前端依赖 + download 工具 + api 函数

**Files:**
- Modify: `frontend/package.json`（安装 `@mdxeditor/editor`）
- Create: `frontend/src/lib/download.ts`
- Modify: `frontend/src/lib/api.ts`（追加 `updateWorkflowSpec`）

**Interfaces:**
- Produces: `triggerDownload(filename, content, mime?)`、`sanitizeFilename(name)`（`frontend/src/lib/download.ts`）；`updateWorkflowSpec(id, name, body): Promise<WorkflowDetail>`（`frontend/src/lib/api.ts`）。

- [ ] **Step 1: 安装依赖**

Run: `cd frontend && npm install @mdxeditor/editor`
Expected: 安装 `@mdxeditor/editor@^4.x`，写入 `package.json` + `package-lock.json`。

- [ ] **Step 2: 创建 download.ts**

创建 `frontend/src/lib/download.ts`：

```ts
/**
 * 触发浏览器下载：把文本内容作为 Blob 生成临时 <a download> 点击。
 * 仅在浏览器端调用（client component 内）。
 */
export function triggerDownload(
  filename: string,
  content: string,
  mime = "text/markdown",
): void {
  const blob = new Blob([content], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * 去掉文件名非法字符（/ \ : * ? " < > |），空名回退为 "spec"。
 */
export function sanitizeFilename(name: string): string {
  return name.replace(/[/\\:*?"<>|]/g, "_").trim() || "spec";
}
```

- [ ] **Step 3: 追加 updateWorkflowSpec 到 api.ts**

在 `frontend/src/lib/api.ts` 的 `saveStepOutput` 函数之后追加：

```ts
export async function updateWorkflowSpec(
  id: string,
  name: string,
  body: string,
) {
  return parseJson<WorkflowDetail>(
    await fetch(`/api/porto/workflows/${encodeURIComponent(id)}/specs`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, body }),
    }),
  );
}
```

- [ ] **Step 4: 类型检查 + lint**

Run: `cd frontend && npx tsc --noEmit && npx eslint src/lib/download.ts src/lib/api.ts`
Expected: 无错误。

- [ ] **Step 5: 提交**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/lib/download.ts frontend/src/lib/api.ts
git commit -m "$(cat <<'EOF'
feat(frontend): 下载工具 + updateWorkflowSpec API

新增 triggerDownload/sanitizeFilename；api.ts 加 PATCH /specs 客户端。
安装 @mdxeditor/editor 依赖。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 前端 SpecMdxEditor 组件

**Files:**
- Create: `frontend/src/components/spec-mdx-editor.tsx`

**Interfaces:**
- Produces: `SpecMdxEditor({ value, onChange }: { value: string; onChange: (markdown: string) => void })`（client 组件，内部直接 import `@mdxeditor/editor`）。调用方必须用 `next/dynamic(..., { ssr: false })` 加载。

- [ ] **Step 1: 创建 spec-mdx-editor.tsx**

创建 `frontend/src/components/spec-mdx-editor.tsx`：

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
}: {
  value: string;
  onChange: (markdown: string) => void;
}) {
  return (
    <MDXEditor
      className="max-h-[60vh] overflow-y-auto rounded-lg border border-zinc-200 bg-white"
      markdown={value}
      onChange={onChange}
      plugins={[
        headingsPlugin(),
        listsPlugin(),
        quotePlugin(),
        thematicBreakPlugin(),
        linkPlugin(),
        codeBlockPlugin(),
        markdownShortcutPlugin(),
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
      ]}
    />
  );
}
```

> 注：`MDXEditor` 与各插件/toolbar 组件均为 `@mdxeditor/editor` 的命名导出。若 tsc 报某 toolbar 组件未导出，按 `node_modules/@mdxeditor/editor` 的实际 `dist` 类型导出调整（执行时 tsc 会即时捕获）。

- [ ] **Step 2: 类型检查 + lint**

Run: `cd frontend && npx tsc --noEmit && npx eslint src/components/spec-mdx-editor.tsx`
Expected: 无错误。

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/spec-mdx-editor.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): SpecMdxEditor 组件（@mdxeditor/editor 封装）

富文本 + markdown 快捷输入，限高 60vh 内滚动。
供 SpecCard 以 next/dynamic ssr:false 加载。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 前端 SpecCard + 回调链集成

**Files:**
- Modify: `frontend/src/components/porto-workbench.tsx`：
  - 顶部加 `next/dynamic` 加载 `SpecMdxEditor` 的常量、import `Download`/`Pencil` 图标、import `triggerDownload`/`sanitizeFilename`/`updateWorkflowSpec`；
  - 新增 `SpecCard` 组件、`SpecActionButton` 辅助组件（置于 `CompletedView` 之前）；
  - `CompletedView`（2359 行）props 加 `onSaveSpec`，specs tab 改用 `SpecCard` + 顶部「下载全部」；
  - `WorkflowPanel`（1840 行）props 加 `onSaveSpec` 并透传给 `CompletedView`；
  - `PortoWorkbench` 顶层加 `onSaveSpec` handler（参照 `onSaveStep`，428 行）并传给 `WorkflowPanel`（554 行）。

**Interfaces:**
- Consumes: `SpecMdxEditor`（Task 4）、`updateWorkflowSpec`/`triggerDownload`/`sanitizeFilename`（Task 3）。
- Produces: `SpecCard({ name, body, onSave })`；`onSave: (name, body) => Promise<boolean>`（true=成功）。

- [ ] **Step 1: 顶部 import 与 dynamic 常量**

在 `frontend/src/components/porto-workbench.tsx` 顶部 import 区（已有 `saveStepOutput` 等 import 的位置）补：

```tsx
import dynamic from "next/dynamic";
import { Download, Pencil } from "lucide-react";
import { sanitizeFilename, triggerDownload } from "@/lib/download";
import { updateWorkflowSpec } from "@/lib/api";
```

在 import 区之后、组件定义之前加动态加载常量：

```tsx
const SpecMdxEditor = dynamic(
  () => import("./spec-mdx-editor").then((m) => m.SpecMdxEditor),
  { ssr: false, loading: () => <Loader2 size={16} className="animate-spin" /> },
);
```

（`Loader2` 已在本文件 import。）

- [ ] **Step 2: 新增 SpecActionButton + SpecCard 组件**

在 `CompletedView` 函数（约 2359 行）之前插入：

```tsx
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
}: {
  name: string;
  body: string;
  onSave: (name: string, body: string) => Promise<boolean>;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(body);
  const [saving, setSaving] = useState(false);

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
      className="rounded-lg border border-zinc-200 bg-zinc-50"
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
    </details>
  );
}
```

- [ ] **Step 3: 改造 CompletedView（props + specs tab）**

把 `CompletedView` 签名改为（约 2359 行）：

```tsx
function CompletedView({
  detail,
  onSaveSpec,
}: {
  detail: WorkflowDetail;
  onSaveSpec: (name: string, body: string) => Promise<boolean>;
}) {
```

在函数体开头（`const understanding = ...` 附近）加：

```tsx
  const projectName = detail.project_name;
```

把 specs tab 分支（原 2447-2473 行那段 `{tab === "specs" ? (...)`）整体替换为：

```tsx
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
                  />
                ))
              : null}
            {!specs || Object.keys(specs).length === 0 ? (
              <p className="text-sm text-zinc-400">无规格输出。</p>
            ) : null}
          </div>
        ) : null}
```

- [ ] **Step 4: WorkflowPanel 透传 onSaveSpec**

在 `WorkflowPanel`（1840 行）的 props 解构加 `onSaveSpec,`；在 props 类型块（约 1865 行 `onSaveStep` 那行之后）加：

```tsx
  onSaveSpec: (name: string, body: string) => Promise<boolean>;
```

把 `CompletedView` 调用（约 1995 行）改为：

```tsx
            <CompletedView detail={detail} onSaveSpec={onSaveSpec} />
```

- [ ] **Step 5: PortoWorkbench 加 handler 并传入**

在 `PortoWorkbench` 内 `onSaveStep`（428 行）之后加：

```tsx
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
```

把 `WorkflowPanel` 调用（约 554 行）补上 prop：

```tsx
            onSaveSpec={onSaveSpec}
```

- [ ] **Step 6: 类型检查 + lint**

Run: `cd frontend && npx tsc --noEmit && npx eslint src/components/porto-workbench.tsx`
Expected: 无错误。（既有 `react-hooks/set-state-in-effect`、`react-hooks/exhaustive-deps` 警告与本次无关，不计入。）

- [ ] **Step 7: 提交**

```bash
git add frontend/src/components/porto-workbench.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): spec 卡片原位编辑 + 单个/全部下载

SpecCard 组件：summary 右侧编辑/下载按钮，编辑态切换为 MDXEditor；
CompletedView specs tab 顶部「下载全部」；PortoWorkbench→WorkflowPanel
→CompletedView→SpecCard 透传 onSaveSpec（PATCH /specs）。

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 端到端验证

**Files:** 无（仅验证）。

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && python -m pytest tests/test_workflow_store.py tests/test_workflow_api.py -q 2>&1 | tail -20`
Expected: Task 1/2 新增测试 PASS，无回归。

- [ ] **Step 2: 前端类型 + lint 全量**

Run: `cd frontend && npx tsc --noEmit && npx eslint src/`
Expected: 无新增错误。

- [ ] **Step 3: 构建（确认 MDXEditor 打包正常）**

Run: `cd frontend && npm run build 2>&1 | tail -30`
Expected: 构建成功，无 SSR 报错（验证 `ssr: false` 生效）。

- [ ] **Step 4: 手动 e2e（用户侧）**

Run: `cd frontend && npm run dev`，浏览器打开一个 **completed** 工作流 → 「规格」tab，验证：
1. 每个 spec 卡片 summary 右侧有「下载」「编辑」按钮；specs tab 顶部有「下载全部」。
2. 点「编辑」→ 卡片展开、内容区变为富文本编辑器；可编辑。
3. 点「保存」→ 内容刷新为编辑后正文，仍处于 completed 视图（evaluate 评分仍在）。
4. 点「取消」→ 恢复原预览。
5. 「下载」→ 下载 `{spec名}.md`；「下载全部」→ 下载 `{项目名}.md`。
6. 断网/出错保存 → 顶部错误提示，编辑态与草稿保留。

Expected: 全部通过。（此步需用户在浏览器确认，因 CLI 环境无法观察渲染。）

- [ ] **Step 5: 如有验证发现的问题，修复后追加提交；无则跳过。**

---

## Self-Review 记录

（写完后已对照 spec 逐节核对：后端接口语义、SpecCard 编辑/预览/下载交互、下载全部格式、回调链、SSR/dynamic、YAGNI 边界均有对应 Task；类型与函数名跨 Task 一致：`update_spec`、`updateWorkflowSpec`、`SpecMdxEditor`、`SpecCard`、`onSaveSpec`、`triggerDownload`、`sanitizeFilename`。）
