# Sessions/Workflows 分页 + 日历 + Healthcheck 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把前端 Sessions 和 Workflows 列表从"按当前 sessionId 过滤"改成"跨 session 按日期倒序分页 + react-day-picker 日历筛选"，并加一个后端离线全局横幅。

**Architecture:** 后端新增 `GET /api/sessions`（memory 表 GROUP BY 聚合）+ 改造 `GET /api/porto/workflows`（加 date/limit/offset/返回 session_id+total+has_more）；前端抽 `SessionList`/`WorkflowList`/`DatePickerPopover` 三个组件替换 Sidebar 内联列表，healthcheck 复用已有 /api/health 轮询、失败时顶部显示横幅 + 禁用输入。

**Tech Stack:** Python/FastAPI/SQLite（后端）、Next.js 16/React 19/TypeScript/Tailwind（前端）、react-day-picker v9 + date-fns（日历）

## Global Constraints

- 后端测试用 `sample_settings` fixture（conftest.py:72，tmp_path + embedding_provider="local" + dimensions=128）和 `_store(tmp_path)` helper（test_workflow_store.py:8）
- 后端测试命令：`cd backend && uv run pytest -q`（注意 shell 里 `VIRTUAL_ENV` 可能指向旧路径，必要时 `env -u VIRTUAL_ENV uv run pytest`）
- 前端无单元测试，用 `cd frontend && npx tsc --noEmit` + `npm run build` 验证
- `frontend/AGENTS.md` 提示 Next.js 16 有 breaking changes，写代码前读 `node_modules/next/dist/docs/`
- spec：`docs/superpowers/specs/2026-07-15-sessions-workflows-pagination-design.md`
- 频繁提交，每个 Task 结束 commit

---

### Task 1: 后端 MemoryStore.list_sessions + GET /api/sessions

**Files:**
- Modify: `backend/src/porto_chatbot/memory/store.py`（加 `list_sessions` 方法）
- Modify: `backend/src/porto_chatbot/api/routes/memory.py`（加 `/api/sessions` route + 模型）
- Test: `backend/tests/test_sessions_api.py`（新建）

**Interfaces:**
- Produces: `MemoryStore.list_sessions(date, limit, offset) -> tuple[list[dict], int]`；`GET /api/sessions?date=&limit=&offset=` 返回 `{items, total, has_more}`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_sessions_api.py
from __future__ import annotations

from porto_chatbot.memory.store import MemoryStore


def _store(sample_settings):
    return MemoryStore(sample_settings)


def test_list_sessions_aggregates_by_session(sample_settings):
    s = _store(sample_settings)
    s.add("s1", "user", "hello")
    s.add("s1", "assistant", "hi there")
    s.add("s2", "user", "another session")
    items, total = s.list_sessions(limit=20, offset=0)
    assert total == 2
    # 按 last_at 倒序：s2 后加 → 在前
    assert items[0]["session_id"] == "s2"
    assert items[1]["session_id"] == "s1"
    assert items[1]["message_count"] == 2
    assert items[1]["first_at"] is not None
    assert items[1]["last_at"] is not None
    # preview = 最后一条消息
    assert items[1]["preview"] == "hi there"


def test_list_sessions_pagination(sample_settings):
    s = _store(sample_settings)
    for i in range(5):
        s.add(f"s{i}", "user", f"msg {i}")
    items, total = s.list_sessions(limit=2, offset=0)
    assert total == 5
    assert len(items) == 2
    items2, _ = s.list_sessions(limit=2, offset=2)
    assert len(items2) == 2
    # 无重叠
    ids = {i["session_id"] for i in items} | {i["session_id"] for i in items2}
    assert len(ids) == 4


def test_list_sessions_date_filter(sample_settings):
    s = _store(sample_settings)
    s.add("s1", "user", "msg")
    # date 过滤 last_at 所在日期；用今天日期应能匹配刚加的
    from datetime import UTC, datetime
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    items, total = s.list_sessions(date=today, limit=20, offset=0)
    assert total == 1
    assert items[0]["session_id"] == "s1"
    # 用一个不存在的日期
    items_empty, total_empty = s.list_sessions(date="2099-01-01", limit=20, offset=0)
    assert total_empty == 0
    assert len(items_empty) == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && env -u VIRTUAL_ENV uv run pytest tests/test_sessions_api.py -v`
Expected: FAIL with `AttributeError: 'MemoryStore' object has no attribute 'list_sessions'`

- [ ] **Step 3: 实现 MemoryStore.list_sessions**

在 `backend/src/porto_chatbot/memory/store.py` 的 `get_messages_ordered` 方法之后（约第 120 行后）加：

```python
    def list_sessions(
        self, date: str | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict], int]:
        """聚合所有 session（按 last_at 倒序），供前端 Sessions 列表分页。

        date 过滤 last_at 所在日期(YYYY-MM-DD)。返回 (items, total)。
        items: [{session_id, first_at, last_at, message_count, preview}]
        preview = 最后一条消息 content 截断 80 字符。
        """
        where: list[str] = []
        params: list[object] = []
        if date:
            where.append("substr(created_at, 1, 10) = ?")
            params.append(date)
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute(
                f"SELECT COUNT(*) FROM (SELECT session_id FROM memories{where_sql} GROUP BY session_id)",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"""SELECT session_id,
                           MIN(created_at) AS first_at,
                           MAX(created_at) AS last_at,
                           COUNT(*) AS message_count
                    FROM memories{where_sql}
                    GROUP BY session_id
                    ORDER BY last_at DESC
                    LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
            items: list[dict] = []
            for r in rows:
                last = conn.execute(
                    "SELECT content FROM memories WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
                    (r["session_id"],),
                ).fetchone()
                preview = (last["content"] if last else "")[:80]
                items.append(
                    {
                        "session_id": r["session_id"],
                        "first_at": r["first_at"],
                        "last_at": r["last_at"],
                        "message_count": r["message_count"],
                        "preview": preview,
                    }
                )
        self.logger.info(
            "sessions list date=%s limit=%s offset=%s total=%s", date, limit, offset, total
        )
        return items, total
```

- [ ] **Step 4: 实现 GET /api/sessions route**

在 `backend/src/porto_chatbot/api/routes/memory.py` 顶部 import 加 `BaseModel`，加模型和 route：

```python
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ...logging_utils import get_component_logger
from ...models import MemorySearchResponse
from ..deps import get_memory

logger = get_component_logger("api")

router = APIRouter()


class SessionItem(BaseModel):
    session_id: str
    first_at: str
    last_at: str
    message_count: int
    preview: str


class SessionListResponse(BaseModel):
    items: list[SessionItem]
    total: int
    has_more: bool


@router.get("/api/sessions", response_model=SessionListResponse)
def list_sessions(date: str | None = None, limit: int = 20, offset: int = 0):
    items, total = get_memory().list_sessions(date=date, limit=limit, offset=offset)
    return SessionListResponse(
        items=[SessionItem(**m) for m in items],
        total=total,
        has_more=offset + len(items) < total,
    )


@router.get("/api/memory/{session_id}")
def list_memory(session_id: str, limit: int = 50):
    logger.info("memory list session_id=%s limit=%s", session_id, limit)
    return {"session_id": session_id, "items": get_memory().list_session(session_id, limit=limit)}


@router.get("/api/memory/search", response_model=MemorySearchResponse)
def search_memory(q: str, session_id: str | None = None, top_k: int = 5):
    logger.info("memory search query_chars=%s session_id=%s top_k=%s", len(q), session_id, top_k)
    return MemorySearchResponse(query=q, results=get_memory().search(q, session_id=session_id, top_k=top_k))
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && env -u VIRTUAL_ENV uv run pytest tests/test_sessions_api.py -v`
Expected: 3 passed

- [ ] **Step 6: 提交**

```bash
git add backend/src/porto_chatbot/memory/store.py backend/src/porto_chatbot/api/routes/memory.py backend/tests/test_sessions_api.py
git commit -m "feat(api): GET /api/sessions 跨 session 分页+日期过滤

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 后端 WorkflowStore + workflow list 改造

**Files:**
- Modify: `backend/src/porto_chatbot/workflow_store.py:85-97`（`list_workflows` 加 date/offset/返回 total）
- Modify: `backend/src/porto_chatbot/api/routes/workflow.py:48-58,160-184`（`WorkflowListItem` 加 session_id；`WorkflowListResponse` 加 total/has_more；route 加 date/offset）
- Modify: `backend/tests/test_workflow_store.py:43-50`（更新 `test_list_filters` 适配 tuple 返回）

**Interfaces:**
- Produces: `WorkflowStore.list_workflows(session_id, status, date, limit, offset) -> tuple[list[dict], int]`；`WorkflowListItem` 加 `session_id: str`；`WorkflowListResponse` 加 `total: int` + `has_more: bool`；`GET /api/porto/workflows?date=&limit=&offset=` 返回 `{items, total, has_more}`

- [ ] **Step 1: 更新 test_list_filters 适配新签名（先改测试）**

把 `backend/tests/test_workflow_store.py` 的 `test_list_filters`（第 43-50 行）改成：

```python
def test_list_filters(tmp_path):
    s = _store(tmp_path)
    w1 = s.create("s1", "p1", "prd", 6, {}, {})
    s.update_status(w1, "completed", current_step="evaluate")
    w2 = s.create("s2", "p2", "prd", 6, {}, {})
    rows, total = s.list_workflows()
    assert total == 2
    assert len(rows) == 2
    rows, total = s.list_workflows(session_id="s1")
    assert total == 1
    assert rows[0]["workflow_id"] == w1
    rows, total = s.list_workflows(status="completed")
    assert total == 1


def test_list_pagination_and_date(tmp_path):
    s = _store(tmp_path)
    for i in range(5):
        s.create(f"s{i}", f"p{i}", "prd", 6, {}, {})
    rows, total = s.list_workflows(limit=2, offset=0)
    assert total == 5
    assert len(rows) == 2
    rows2, _ = s.list_workflows(limit=2, offset=2)
    assert len(rows2) == 2
    # 倒序：offset=0 的应比 offset=2 的新
    assert rows[0]["created_at"] >= rows2[0]["created_at"]
    # date 过滤：今天创建的
    from datetime import UTC, datetime
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    rows, total = s.list_workflows(date=today, limit=20, offset=0)
    assert total == 5
    rows, total = s.list_workflows(date="2099-01-01", limit=20, offset=0)
    assert total == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && env -u VIRTUAL_ENV uv run pytest tests/test_workflow_store.py -v`
Expected: FAIL（`list_workflows` 返回 list 不是 tuple，unpack 失败）

- [ ] **Step 3: 改造 WorkflowStore.list_workflows**

把 `backend/src/porto_chatbot/workflow_store.py:85-97` 的 `list_workflows` 方法替换为：

```python
    def list_workflows(
        self,
        session_id=None,
        status=None,
        date=None,
        limit=50,
        offset=0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[object] = []
        if session_id:
            where.append("session_id=?")
            params.append(session_id)
        if status:
            where.append("status=?")
            params.append(status)
        if date:
            where.append("substr(created_at, 1, 10)=?")
            params.append(date)
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        with self._conn() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM workflows{where_sql}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM workflows{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [dict(r) for r in rows], total
```

- [ ] **Step 4: 改造 workflow route（模型 + endpoint）**

在 `backend/src/porto_chatbot/api/routes/workflow.py`：

把 `WorkflowListItem`（第 48-54 行）改成：

```python
class WorkflowListItem(BaseModel):
    workflow_id: str
    session_id: str
    project_name: str | None
    status: str
    current_step: str | None
    created_at: str
    score: int | None = None
```

把 `WorkflowListResponse`（第 57-58 行）改成：

```python
class WorkflowListResponse(BaseModel):
    items: list[WorkflowListItem]
    total: int
    has_more: bool
```

把 `list_workflows` endpoint（第 160-184 行）改成：

```python
@router.get("/api/porto/workflows", response_model=WorkflowListResponse)
def list_workflows(
    session_id: str | None = None,
    status: str | None = None,
    date: str | None = None,
    limit: int = 20,
    offset: int = 0,
):
    """列表(按 created_at DESC),可按 session_id / status / date 过滤,分页。

    每条附 evaluation score(若有)——list 不展开完整 outputs,避免大 payload。
    """
    store = get_workflow_store()
    rows, total = store.list_workflows(
        session_id=session_id, status=status, date=date, limit=limit, offset=offset
    )
    items: list[WorkflowListItem] = []
    for r in rows:
        score = None
        outs = store.get_outputs(r["workflow_id"])
        if "evaluate" in outs:
            score = (outs["evaluate"]["output"].get("evaluation") or {}).get("score")
        items.append(
            WorkflowListItem(
                workflow_id=r["workflow_id"],
                session_id=r["session_id"],
                project_name=r["project_name"],
                status=r["status"],
                current_step=r["current_step"],
                created_at=r["created_at"],
                score=score,
            )
        )
    return WorkflowListResponse(
        items=items, total=total, has_more=offset + len(items) < total
    )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && env -u VIRTUAL_ENV uv run pytest tests/test_workflow_store.py tests/test_workflow_api.py -v`
Expected: all passed

- [ ] **Step 6: 运行全量后端测试确认无回归**

Run: `cd backend && env -u VIRTUAL_ENV uv run pytest -q`
Expected: all passed（若 test_workflow_api.py 里断言了旧响应结构需同步修，按报错修）

- [ ] **Step 7: 提交**

```bash
git add backend/src/porto_chatbot/workflow_store.py backend/src/porto_chatbot/api/routes/workflow.py backend/tests/test_workflow_store.py
git commit -m "feat(api): workflows 列表分页+日期过滤+返回 session_id

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 前端装依赖 + types + api

**Files:**
- Modify: `frontend/package.json`（装 react-day-picker + date-fns）
- Modify: `frontend/src/lib/types.ts`（加 SessionItem/Paginated，WorkflowListItem 加 session_id）
- Modify: `frontend/src/lib/api.ts`（加 listSessions，改 listWorkflows 签名）
- Modify: `frontend/src/components/porto-workbench.tsx`（临时更新 listWorkflows 调用点适配新签名，保持 tsc 通过）

**Interfaces:**
- Produces: `listSessions(date?, limit?, offset?) -> Promise<Paginated<SessionItem>>`；`listWorkflows(params?) -> Promise<Paginated<WorkflowListItem>>`

- [ ] **Step 1: 装依赖**

Run:
```bash
cd frontend && npm install react-day-picker date-fns
```
Expected: 装入 `react-day-picker@^9` + `date-fns@^4`

- [ ] **Step 2: 更新 types.ts**

在 `frontend/src/lib/types.ts` 的 `WorkflowListItem` 类型加 `session_id`，并在文件末尾追加 `SessionItem` + `Paginated`：

把 `WorkflowListItem`（约第 204-211 行）改成：

```ts
export type WorkflowListItem = {
  workflow_id: string;
  session_id: string;
  project_name: string | null;
  status: WorkflowStatus;
  current_step: WorkflowStepName | null;
  created_at: string;
  score: number | null;
};
```

文件末尾追加：

```ts
export type SessionItem = {
  session_id: string;
  first_at: string;
  last_at: string;
  message_count: number;
  preview: string;
};

export type Paginated<T> = {
  items: T[];
  total: number;
  has_more: boolean;
};
```

- [ ] **Step 3: 更新 api.ts**

在 `frontend/src/lib/api.ts`：

把 `listWorkflows`（约第 110-115 行）改成：

```ts
export async function listWorkflows(params?: {
  sessionId?: string;
  date?: string;
  limit?: number;
  offset?: number;
}) {
  const q = new URLSearchParams();
  const p = params ?? {};
  if (p.sessionId) q.set("session_id", p.sessionId);
  if (p.date) q.set("date", p.date);
  q.set("limit", String(p.limit ?? 20));
  q.set("offset", String(p.offset ?? 0));
  return parseJson<Paginated<WorkflowListItem>>(
    await fetch(`/api/porto/workflows?${q.toString()}`),
  );
}
```

在 `listWorkflows` 之后加 `listSessions`：

```ts
export async function listSessions(date?: string, limit = 20, offset = 0) {
  const q = new URLSearchParams();
  if (date) q.set("date", date);
  q.set("limit", String(limit));
  q.set("offset", String(offset));
  return parseJson<Paginated<SessionItem>>(
    await fetch(`/api/sessions?${q.toString()}`),
  );
}
```

在顶部 import 块加 `Paginated` 和 `SessionItem`：

```ts
import type {
  AgentConfig,
  AppSettings,
  ChatResponse,
  HealthSnapshot,
  IndexJobStatus,
  KbStats,
  MemoryRecord,
  Paginated,
  RagConfig,
  SessionItem,
  SourceChunk,
  WorkflowDetail,
  WorkflowListItem,
  WorkflowStepName,
} from "./types";
```

- [ ] **Step 4: 临时更新 porto-workbench.tsx 的 listWorkflows 调用点**

`porto-workbench.tsx` 里有两处 `listWorkflows(sessionId)`（`refreshWorkflowList` 和加载历史的 useEffect），临时改成新签名保持 tsc 通过（Task 7 会替换成新组件）：

把 `refreshWorkflowList` 里的 `const result = await listWorkflows(sessionId);` 改成：

```ts
const result = await listWorkflows({ sessionId });
```

把加载 workflow 历史的 useEffect 里 `const result = await listWorkflows(sessionId);` 改成：

```ts
const result = await listWorkflows({ sessionId });
```

注意：旧代码 `setWorkflowList(result.items)` 仍有效（`Paginated<WorkflowListItem>` 有 items）。但 `result` 现在还有 total/has_more，旧代码忽略它们，OK。

- [ ] **Step 5: tsc 验证**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 6: 提交**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/lib/types.ts frontend/src/lib/api.ts frontend/src/components/porto-workbench.tsx
git commit -m "feat(frontend): types/api 加 sessions + 改 listWorkflows 签名

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 前端 DatePickerPopover 组件

**Files:**
- Create: `frontend/src/components/date-picker-popover.tsx`

**Interfaces:**
- Produces: `DatePickerPopover({ date, onSelect })` —— `date` 是 "YYYY-MM-DD" 或 ""，`onSelect(dateStr)` 回调，"" 表示清除

- [ ] **Step 1: 创建组件**

```tsx
// frontend/src/components/date-picker-popover.tsx
"use client";

import { format } from "date-fns";
import { zhCN } from "date-fns/locale";
import { Calendar, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { DayPicker } from "react-day-picker";

export function DatePickerPopover({
  date,
  onSelect,
}: {
  date: string;
  onSelect: (date: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const selected = date ? new Date(date + "T00:00:00") : undefined;

  return (
    <div className="relative" ref={ref}>
      <div className="flex items-center">
        <button
          type="button"
          className={`rounded-md p-1 ${date ? "bg-zinc-950 text-white" : "text-zinc-500 hover:bg-zinc-100"}`}
          onClick={() => setOpen((v) => !v)}
          title={date ? `筛选: ${date}` : "按日期筛选"}
        >
          <Calendar size={14} />
        </button>
        {date ? (
          <button
            type="button"
            className="ml-0.5 text-zinc-400 hover:text-zinc-600"
            onClick={() => onSelect("")}
            title="清除日期"
          >
            <X size={12} />
          </button>
        ) : null}
      </div>
      {open ? (
        <div className="absolute right-0 z-50 mt-1 rounded-md border border-zinc-200 bg-white p-2 shadow-lg">
          <DayPicker
            mode="single"
            locale={zhCN}
            selected={selected}
            onSelect={(d) => {
              if (d) {
                onSelect(format(d, "yyyy-MM-dd"));
                setOpen(false);
              }
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 2: tsc 验证**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/date-picker-popover.tsx
git commit -m "feat(frontend): DatePickerPopover 组件(react-day-picker)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 前端 SessionList 组件

**Files:**
- Create: `frontend/src/components/session-list.tsx`

**Interfaces:**
- Consumes: `listSessions` from `@/lib/api`，`SessionItem` from `@/lib/types`，`DatePickerPopover`
- Produces: `SessionList({ activeSessionId, onPickSession })` —— 滚动分页 + calendar

- [ ] **Step 1: 创建组件**

```tsx
// frontend/src/components/session-list.tsx
"use client";

import { Loader2, MessageSquare } from "lucide-react";
import { useEffect, useState } from "react";
import { listSessions } from "@/lib/api";
import type { SessionItem } from "@/lib/types";
import { DatePickerPopover } from "./date-picker-popover";

export function SessionList({
  activeSessionId,
  onPickSession,
}: {
  activeSessionId: string;
  onPickSession: (sessionId: string) => void;
}) {
  const [items, setItems] = useState<SessionItem[]>([]);
  const [date, setDate] = useState("");
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const data = await listSessions(date || undefined, 20, 0);
        if (cancelled) return;
        setItems(data.items);
        setOffset(data.items.length);
        setHasMore(data.has_more);
      } catch {
        /* 非关键 */
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [date]);

  async function loadMore() {
    if (loading || !hasMore) return;
    setLoading(true);
    try {
      const data = await listSessions(date || undefined, 20, offset);
      setItems((prev) => [...prev, ...data.items]);
      setOffset((o) => o + data.items.length);
      setHasMore(data.has_more);
    } catch {
      /* 非关键 */
    } finally {
      setLoading(false);
    }
  }

  function onScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 20) {
      loadMore();
    }
  }

  return (
    <section className="mt-4 rounded-lg border border-zinc-200 bg-white p-3">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-sm font-medium">
          <MessageSquare size={15} /> Sessions
        </h2>
        <DatePickerPopover date={date} onSelect={setDate} />
      </div>
      <div className="max-h-72 space-y-2 overflow-y-auto" onScroll={onScroll}>
        {items.map((s) => (
          <button
            key={s.session_id}
            className={`block w-full rounded-md border p-2 text-left ${
              s.session_id === activeSessionId
                ? "border-zinc-950 bg-zinc-50"
                : "border-zinc-200 hover:bg-zinc-50"
            }`}
            onClick={() => onPickSession(s.session_id)}
          >
            <div className="mb-1 flex items-center justify-between gap-2 text-xs">
              <span className="truncate font-medium">{s.session_id}</span>
              <span className="truncate text-zinc-400">
                {s.last_at.slice(0, 10)}
              </span>
            </div>
            <p className="line-clamp-2 text-xs leading-5 text-zinc-600">
              {s.preview}
            </p>
            <span className="text-xs text-zinc-400">{s.message_count} 条</span>
          </button>
        ))}
        {loading ? (
          <div className="flex justify-center py-2">
            <Loader2 size={14} className="animate-spin text-zinc-400" />
          </div>
        ) : null}
        {!loading && items.length === 0 ? (
          <p className="text-sm text-zinc-400">暂无 session。</p>
        ) : null}
        {!loading && items.length > 0 && !hasMore ? (
          <p className="py-1 text-center text-xs text-zinc-400">无更多</p>
        ) : null}
      </div>
    </section>
  );
}
```

- [ ] **Step 2: tsc 验证**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/session-list.tsx
git commit -m "feat(frontend): SessionList 组件(分页+日历)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: 前端 WorkflowList 组件

**Files:**
- Create: `frontend/src/components/workflow-list.tsx`

**Interfaces:**
- Consumes: `listWorkflows` from `@/lib/api`，`WorkflowListItem`/`WorkflowStepName` from `@/lib/types`，`DatePickerPopover`
- Produces: `WorkflowList({ activeWorkflowId, onPickWorkflow })` —— 滚动分页 + calendar

- [ ] **Step 1: 创建组件**

```tsx
// frontend/src/components/workflow-list.tsx
"use client";

import { Braces, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { listWorkflows } from "@/lib/api";
import type { WorkflowListItem } from "@/lib/types";
import { DatePickerPopover } from "./date-picker-popover";

const STEP_LABELS: Record<string, string> = {
  retrieve: "检索",
  understand: "理解",
  identify: "子系统",
  generate: "规格",
  evaluate: "评估",
};

export function WorkflowList({
  activeWorkflowId,
  onPickWorkflow,
}: {
  activeWorkflowId: string | null;
  onPickWorkflow: (id: string) => void;
}) {
  const [items, setItems] = useState<WorkflowListItem[]>([]);
  const [date, setDate] = useState("");
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const data = await listWorkflows({ date: date || undefined, limit: 20, offset: 0 });
        if (cancelled) return;
        setItems(data.items);
        setOffset(data.items.length);
        setHasMore(data.has_more);
      } catch {
        /* 非关键 */
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [date]);

  async function loadMore() {
    if (loading || !hasMore) return;
    setLoading(true);
    try {
      const data = await listWorkflows({ date: date || undefined, limit: 20, offset });
      setItems((prev) => [...prev, ...data.items]);
      setOffset((o) => o + data.items.length);
      setHasMore(data.has_more);
    } catch {
      /* 非关键 */
    } finally {
      setLoading(false);
    }
  }

  function onScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 20) {
      loadMore();
    }
  }

  return (
    <section className="mt-4 rounded-lg border border-zinc-200 bg-white p-3">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-sm font-medium">
          <Braces size={15} /> Workflows
        </h2>
        <DatePickerPopover date={date} onSelect={setDate} />
      </div>
      <div className="max-h-72 space-y-2 overflow-y-auto" onScroll={onScroll}>
        {items.map((wf) => {
          const active = wf.workflow_id === activeWorkflowId;
          const stepLabel = wf.current_step
            ? STEP_LABELS[wf.current_step] ?? wf.current_step
            : "—";
          return (
            <button
              key={wf.workflow_id}
              className={`block w-full rounded-md border p-2 text-left ${
                active
                  ? "border-zinc-950 bg-zinc-50"
                  : "border-zinc-200 hover:bg-zinc-50"
              }`}
              onClick={() => onPickWorkflow(wf.workflow_id)}
            >
              <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                <span className="truncate font-medium">
                  {wf.project_name || wf.workflow_id.slice(0, 8)}
                </span>
                <span className="truncate text-zinc-400">{stepLabel}</span>
              </div>
              <div className="flex items-center justify-between text-xs text-zinc-500">
                <span>{wf.status}</span>
                <span className="truncate">{wf.created_at.slice(0, 10)}</span>
              </div>
            </button>
          );
        })}
        {loading ? (
          <div className="flex justify-center py-2">
            <Loader2 size={14} className="animate-spin text-zinc-400" />
          </div>
        ) : null}
        {!loading && items.length === 0 ? (
          <p className="text-sm text-zinc-400">暂无拆解记录。</p>
        ) : null}
        {!loading && items.length > 0 && !hasMore ? (
          <p className="py-1 text-center text-xs text-zinc-400">无更多</p>
        ) : null}
      </div>
    </section>
  );
}
```

- [ ] **Step 2: tsc 验证**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/workflow-list.tsx
git commit -m "feat(frontend): WorkflowList 组件(分页+日历)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: 前端 Sidebar 接入新组件 + 移除旧内联列表

**Files:**
- Modify: `frontend/src/components/porto-workbench.tsx`（Sidebar 用 `<SessionList>`/`<WorkflowList>` 替换内联 Chat Records/Workflows section；移除 `workflowList` state 和 `refreshWorkflowList`/加载 workflow 历史 useEffect；移除 Sidebar 的 `memoryItems`/`workflows`/`workflowId`/`onPickWorkflow` props）

**Interfaces:**
- Consumes: `SessionList`、`WorkflowList` from Task 5/6

- [ ] **Step 1: 改 Sidebar 函数签名 + 替换两个 section**

在 `porto-workbench.tsx` 顶部 import 加：

```ts
import { SessionList } from "@/components/session-list";
import { WorkflowList } from "@/components/workflow-list";
```

把 `Sidebar` 函数的 props 类型（约第 623-655 行）简化——移除 `memoryItems`、`workflows`、`onRunMemorySearch`、`memoryQuery`、`setMemoryQuery`（SessionList/WorkflowList 内部自管数据与分页）。**保留** `workflowId` 和 `onPickWorkflow`（WorkflowList 需要）。新的 `Sidebar` 签名：

```ts
function Sidebar({
  busy,
  kbStats,
  mode,
  onPickWorkflow,
  sessionId,
  view,
  workflowId,
  setMode,
  setSessionId,
  setView,
}: {
  busy: boolean;
  kbStats: KbStats | null;
  mode: Mode;
  onPickWorkflow: (id: string) => void;
  sessionId: string;
  view: View;
  workflowId: string | null;
  setMode: (value: Mode) => void;
  setSessionId: (value: string) => void;
  setView: (value: View) => void;
}) {
```

在 Sidebar 的 JSX 里：
- 删除原 "Session" section（含 Session ID 输入框 + 记忆搜索）—— Session ID 输入框保留，但移到一个精简的 section；记忆搜索框移除（memory search 仍可从 Inspector 用，或本次移除——spec 说 memoryItems 保留给 Inspector，但记忆搜索框是 Sidebar 的，移除它）
- 删除原 "Chat Records" section（用 `<SessionList>` 替换）
- 删除原 "Workflows" section（用 `<WorkflowList>` 替换）

把 Sidebar 的 Session 输入 + Chat Records + Workflows 三个 section（约第 717-815 行）替换为：

```tsx
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

      <section className="mt-4 rounded-lg border border-zinc-200 bg-white p-3">
        <h2 className="mb-3 flex items-center gap-2 text-sm font-medium">
          <History size={15} />
          Session
        </h2>
        <label className="block text-xs text-zinc-500">Session ID</label>
        <input
          className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-1.5 text-sm outline-none focus:border-zinc-400"
          value={sessionId}
          onChange={(event) => setSessionId(event.target.value)}
        />
      </section>

      <SessionList
        activeSessionId={sessionId}
        onPickSession={(sid) => {
          setSessionId(sid);
          setView("workbench");
        }}
      />

      <WorkflowList activeWorkflowId={workflowId} onPickWorkflow={onPickWorkflow} />
```

- [ ] **Step 2: 从 PortoWorkbench 传真实 onPickWorkflow + 移除旧 state**

在 `PortoWorkbench` 函数里：
- 删除 `const [workflowList, setWorkflowList] = useState<WorkflowListItem[]>([]);`（约第 208 行）
- 删除 `refreshWorkflowList` 函数（约第 217-224 行）
- 删除加载 workflow 历史的 useEffect（约第 226-241 行）
- 删除 workflow 轮询 useEffect 里的 `void refreshWorkflowList();` 调用（约第 267 行，终态刷新历史列表——不再需要，WorkflowList 自己管）
- 删除 `runWorkflowAction` 里的 `void refreshWorkflowList();`（约第 464 行）
- **保留** `onPickWorkflow` 函数（约第 510-530 行）——WorkflowList 需要这个 handler（setMode + setWorkflowId + getWorkflow + setWorkflowDetail + setDraft）

更新 `<Sidebar>` 调用（约第 535-551 行），移除已删的 props（memoryItems/workflows/onRunMemorySearch/memoryQuery/setMemoryQuery），加 `workflowId` 和 `onPickWorkflow`：

```tsx
        <Sidebar
          busy={Boolean(busyLabel)}
          kbStats={kbStats}
          mode={mode}
          onPickWorkflow={onPickWorkflow}
          sessionId={sessionId}
          view={view}
          workflowId={workflowId}
          setMode={setMode}
          setSessionId={setSessionId}
          setView={setView}
        />
```

- [ ] **Step 3: 移除不再使用的 import**

`porto-workbench.tsx` 顶部 import 里：
- `listWorkflows` 不再在 PortoWorkbench 里直接用（WorkflowList 内部用）——从 import 移除
- `WorkflowListItem` 类型不再在 PortoWorkbench 用——从 import 移除
- `History` 图标如果只剩 Session section 用则保留；`Search` 图标如果记忆搜索移除后没用了则移除

保留 `memoryItems` state 和 `refreshMemory`——Inspector 的 Chat Records section（约第 2488-2503 行）仍用 `memoryItems`。

- [ ] **Step 4: tsc 验证**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors（如有 unused import 警告，按提示清理）

- [ ] **Step 5: build 验证**

Run: `cd frontend && npm run build`
Expected: ✓ Compiled successfully

- [ ] **Step 6: 提交**

```bash
git add frontend/src/components/porto-workbench.tsx
git commit -m "refactor(frontend): Sidebar 接入 SessionList/WorkflowList 组件

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: 前端 healthcheck 离线横幅

**Files:**
- Modify: `frontend/src/components/porto-workbench.tsx`（health useEffect 改造 + 顶部横幅 + 禁用 Composer）

**Interfaces:**
- Consumes: 已有 `getHealth` + `health` state

- [ ] **Step 1: 加 backendOnline state + 改造 health useEffect**

在 `PortoWorkbench` 函数里，`const [health, setHealth] = useState<HealthSnapshot | null>(null);` 之后加：

```ts
  const [backendOnline, setBackendOnline] = useState(true);
```

把 health 轮询 useEffect（约第 305-322 行）改成：

```ts
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
```

- [ ] **Step 2: 加顶部离线横幅**

在 `<AssistantRuntimeProvider>` 内、主布局 `<div>` 之前加横幅。把 return（约第 532-534 行）改成：

```tsx
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="flex min-h-screen flex-col">
        {!backendOnline ? (
          <div className="flex items-center justify-center gap-2 bg-rose-600 px-4 py-2 text-sm font-medium text-white">
            <Loader2 size={14} className="animate-spin" />
            后端未连接，请检查 make status
          </div>
        ) : null}
        <div className="grid flex-1 grid-cols-1 bg-zinc-100 text-zinc-950 lg:grid-cols-[300px_minmax(0,1fr)_380px]">
```

并在最外层 `</div>` 前闭合新增的 `<div className="flex min-h-screen flex-col">`——即原来最外层的 `<div className="grid min-h-screen ...">` 现在变成内层，外面包一层 flex 容器。结尾的 `</div>` 结构同步加一层。

原结尾（约第 618-620 行）：

```tsx
        <Inspector inspector={inspector} memoryItems={memoryItems} />
      </div>
    </AssistantRuntimeProvider>
  );
```

改成：

```tsx
        <Inspector inspector={inspector} memoryItems={memoryItems} />
        </div>
      </div>
    </AssistantRuntimeProvider>
  );
```

- [ ] **Step 3: 禁用 Composer（后端离线时）**

`Composer` 函数（约第 929 行）加 `disabled` prop：

```tsx
function Composer({ disabled }: { disabled: boolean }) {
  return (
    <ComposerPrimitive.Root className="mx-auto flex max-w-4xl items-end gap-2">
      <ComposerPrimitive.Input
        className="min-h-12 flex-1 resize-none rounded-xl border border-zinc-200 bg-white px-3 py-3 text-sm outline-none focus:border-zinc-400 disabled:opacity-50"
        placeholder="询问 ~/.scv/analysis 知识库..."
        rows={1}
        disabled={disabled}
      />
      <ComposerPrimitive.Send
        className="flex size-12 items-center justify-center rounded-xl bg-zinc-950 text-white transition hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40"
        disabled={disabled}
      >
        <Send size={17} />
      </ComposerPrimitive.Send>
    </ComposerPrimitive.Root>
  );
}
```

`ThreadView` 里 `<Composer />` 调用（约第 839 行）改成 `<Composer disabled={!backendOnline} />`。但 `ThreadView` 当前只接收 `error` prop，需要加 `backendOnline` 或直接传 `disabled`。把 `ThreadView` 签名和调用改成：

```tsx
function ThreadView({ error, disabled }: { error: string; disabled: boolean }) {
```

调用处（约第 594 行）：

```tsx
            <ThreadView error={error} disabled={!backendOnline} />
```

- [ ] **Step 4: tsc 验证**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 5: build 验证**

Run: `cd frontend && npm run build`
Expected: ✓ Compiled successfully

- [ ] **Step 6: 提交**

```bash
git add frontend/src/components/porto-workbench.tsx
git commit -m "feat(frontend): healthcheck 离线横幅 + 禁用输入

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: 端到端验证

- [ ] **Step 1: 重启前后端**

Run:
```bash
make -C /Users/yuhaochen/Documents/codebase/projanvil/Porto restart
```

- [ ] **Step 2: 验证后端 API**

Run:
```bash
curl -s "http://127.0.0.1:8100/api/sessions" | python3 -m json.tool | head -20
curl -s "http://127.0.0.1:8100/api/porto/workflows?limit=5" | python3 -m json.tool | head -20
curl -s "http://127.0.0.1:8100/api/porto/workflows?date=2026-07-13" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'total={d[\"total\"]} has_more={d[\"has_more\"]}')"
```
Expected: sessions 返回 4 个 session（含 porto-2026-07-13）；workflows 返回 2 个，每项含 session_id 字段；date 过滤生效

- [ ] **Step 3: 浏览器验证**

访问 `http://localhost:3000`：
- Sessions 列表显示历史 session（porto-2026-07-13 等），滚动可加载更多
- 点 Sessions 的 calendar 图标 → 选 2026-07-13 → 列表过滤到该日期
- 点某个 session → 切到该 session，聊天线程加载历史对话
- Workflows 列表显示历史 workflow，calendar 过滤生效
- 点某 workflow → 加载详情
- 停后端 `make backend-stop` → 顶部出现红色"后端未连接"横幅 + 输入框禁用
- 重启后端 → 横幅消失、输入恢复

- [ ] **Step 4: 运行全量后端测试**

Run: `cd backend && env -u VIRTUAL_ENV uv run pytest -q`
Expected: all passed
