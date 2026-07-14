# Sessions / Workflows 列表分页 + 日历查看 设计文档

> 日期：2026-07-15
> 状态：待审查

## 1. 背景与目标

当前前端 Sidebar 的 "Chat Records" 和 "Workflows" 两个列表都按**当前 sessionId** 过滤：

- `listMemory(sessionId)` → 只返回当前 session 的消息
- `listWorkflows(sessionId)` → 只返回当前 session 的 workflow

前端默认 `sessionId = porto-${今天日期}`（[porto-workbench.tsx](frontend/src/components/porto-workbench.tsx)），日期一变就连到一个新的空 session，历史数据（在旧 session 里）全部看不到。用户不知道有哪些旧 session，也无法按日期浏览历史。

**目标**：把两个列表改成**跨 session、按创建日期倒序分页下拉 + calendar 日期选择**，让用户能滚动浏览和按日期查找历史。

**关键概念澄清**：用户说的 "chat record" 指的是 **session**（一个聊天会话），不是单条消息。所以 "Chat Records" section 改为 "Sessions" 列表，每项是一个 session。

## 2. 整体改造

| 列表 | 改造前 | 改造后 |
|---|---|---|
| Chat Records（单条消息） | `listMemory(sessionId)` 当前 session 消息 | **Sessions 列表**：`listSessions()` 跨 session，按 last_at 倒序分页 |
| Workflows | `listWorkflows(sessionId)` 当前 session | `listWorkflows()` 跨 session，按 created_at 倒序分页，返回 session_id |

两个列表都支持：
- 滚动到底部自动加载下一页（分页下拉）
- calendar 日期选择按钮（react-day-picker），选日期过滤该日期的数据

## 3. 后端 API

### 3.1 新增 `GET /api/sessions` —— 列出所有 session

从 `memory.sqlite3` 的 `memories` 表聚合（`GROUP BY session_id`）。

```
GET /api/sessions?date=2026-07-13&limit=20&offset=0
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `date` | string (YYYY-MM-DD) | 可选，过滤 `last_at` 所在日期等于该值的 session |
| `limit` | int | 分页大小，默认 20 |
| `offset` | int | 偏移量，默认 0 |

返回（按 `last_at` 倒序）：
```json
{
  "items": [
    {
      "session_id": "porto-2026-07-13",
      "first_at": "2026-07-13T07:55:43+00:00",
      "last_at": "2026-07-13T14:00:38+00:00",
      "message_count": 11,
      "preview": "imed-process 是做什么的..."
    }
  ],
  "total": 4,
  "has_more": false
}
```

- `preview`：该 session 最后一条消息的 content，截断到 80 字符
- `has_more`：`offset + len(items) < total`

**后端实现**：`MemoryStore` 新增 `list_sessions(date, limit, offset)` 方法，用 SQL `GROUP BY session_id` 聚合，`ORDER BY last_at DESC`，`LIMIT/OFFSET` 分页。新增 API route `backend/src/porto_chatbot/api/routes/memory.py` 加 `GET /api/sessions`。

### 3.2 改造 `GET /api/porto/workflows`

当前按 `session_id` 过滤。改为支持跨 session 分页 + 日期过滤：

```
GET /api/porto/workflows?date=2026-07-13&limit=20&offset=0
```

| 参数 | 类型 | 说明 |
|---|---|---|
| `session_id` | string | 可选，向后兼容，传则过滤该 session |
| `date` | string (YYYY-MM-DD) | 可选，按 `created_at` 所在日期过滤 |
| `limit` | int | 默认 20 |
| `offset` | int | 默认 0 |

返回项**新增 `session_id` 字段**（当前 `WorkflowListItem` 没有），按 `created_at` 倒序，响应加 `total` + `has_more`。

**后端实现**：`WorkflowStore.list` 改造，支持 `date`/`limit`/`offset` 参数，`ORDER BY created_at DESC`，不传 `session_id` 时返回所有。`WorkflowListItem` 模型加 `session_id` 字段。

## 4. 前端

### 4.1 组件拆分

当前 `porto-workbench.tsx` 已 2500+ 行。把两个列表抽成独立组件，各自管理分页 + calendar 状态，降低主组件复杂度：

- `SessionList`：Sessions 列表（分页 + calendar + 点击切 session）
- `WorkflowList`：Workflows 列表（分页 + calendar + 点击加载 workflow）
- `DatePickerPopover`：react-day-picker 封装，两个列表复用

Sidebar 里用 `<SessionList />` / `<WorkflowList />` 替代当前内联列表。

### 4.2 SessionList 组件

- **数据源**：`listSessions(date?, limit=20, offset)`
- **每项显示**：session_id（如 `porto-2026-07-13`）+ message_count 条 + preview（截断 2 行）+ last_at 时间
- **点击**：`onPickSession(session_id)` → `setSessionId(session_id)` → 已有的历史回填 useEffect 自动加载该 session 完整对话到聊天线程 → `setView("workbench")`
- `memoryItems` 仍保留：Inspector 右栏的 Chat Records 还用（显示当前 session 的消息）

### 4.3 WorkflowList 组件

- **数据源**：`listWorkflows(date?, limit=20, offset)`（不传 session_id）
- **每项显示**：project_name / workflow_id（截断）+ status + step label + created_at + session_id 标注
- **点击**：`onPickWorkflow(id)`（已有逻辑，加载该 workflow detail）

### 4.4 分页交互（两个列表通用）

每个列表独立维护状态：
```ts
{ items: T[], date: string, offset: number, has_more: boolean, loading: boolean }
```

- 滚动到列表底部（`onScroll` 判断 scrollTop + clientHeight >= scrollHeight）→ `has_more && !loading` 时加载下一页：`offset += limit`，items 追加
- `date` 变化 → reset `offset=0`、items 清空、重新加载
- 顶部 loading 指示（Loader2 旋转）
- `has_more=false` 时底部显示"无更多"

### 4.5 calendar（react-day-picker）

`DatePickerPopover` 组件：

- 触发器：lucide `Calendar` 图标按钮，放在列表标题栏右侧
- 点击 → 弹出 popover（absolute 定位，z-index 高于列表），内含 `react-day-picker` 的 `DayPicker`
- 选日期 → `onChange` → 回调 `onSelectDate(dateStr)` → 列表 reset + 按该日期过滤
- 选中日期时按钮高亮 + 旁边出现"✕ 清除"按钮 → `onSelectDate("")` 回到全部
- 点击 popover 外部 → 关闭（useEffect 监听 document click）

### 4.6 api.ts 新增 / 改签名

```ts
// 新增
listSessions(date?: string, limit?: number, offset?: number)
  → GET /api/sessions?date=&limit=&offset=
  → { items: SessionItem[], total, has_more }

// 改签名（sessionId 改可选，加 date/limit/offset）
listWorkflows(params?: { sessionId?: string; date?: string; limit?: number; offset?: number })
  → GET /api/porto/workflows?session_id=&date=&limit=&offset=
  → { items: WorkflowListItem[], total, has_more }
```

### 4.7 types.ts 新增

```ts
type SessionItem = {
  session_id: string;
  first_at: string;
  last_at: string;
  message_count: number;
  preview: string;
};

type Paginated<T> = {
  items: T[];
  total: number;
  has_more: boolean;
};

// WorkflowListItem 加 session_id 字段
```

## 5. 依赖

前端新增：
- `react-day-picker`（v9，支持 React 19）
- `date-fns`（react-day-picker v9 的 peer dependency，用于日期格式化/解析）

后端无新增依赖。

## 6. 数据流

```
用户滚动到底 / 选日期
  → 前端调 listSessions / listWorkflows(date, limit, offset)
  → 后端 SQL 查询（GROUP BY / ORDER BY / LIMIT OFFSET）
  → 返回 { items, total, has_more }
  → 前端追加 / 替换 items，更新 offset/has_more

用户点击 session
  → setSessionId(item.session_id)
  → 已有的 useEffect 触发 listMemory + runtime.thread.reset（加载该 session 对话）

用户点击 workflow
  → onPickWorkflow(id) → getWorkflow(id) → 渲染 detail（已有逻辑）
```

## 7. 不在本次范围（YAGNI）

- 全文搜索 chat record / workflow（只做日期过滤 + 滚动分页）
- session 重命名 / 删除
- workflow 批量操作
- calendar 的日期范围选择（只选单日，可清除）
- 跨 session 的消息合并视图
- healthcheck 机制（独立功能，另行实现）

## 8. 实现顺序（建议）

1. 后端：`MemoryStore.list_sessions` + `GET /api/sessions` route
2. 后端：`WorkflowStore.list` 改造（date/limit/offset/倒序）+ `WorkflowListItem` 加 session_id + route 改造
3. 后端：写测试验证分页/日期过滤
4. 前端：装 react-day-picker + date-fns
5. 前端：`api.ts` / `types.ts` 新增类型和函数
6. 前端：`DatePickerPopover` 组件
7. 前端：`SessionList` 组件（替换 Chat Records section）
8. 前端：`WorkflowList` 组件
9. 前端：Sidebar 接入两个新组件，移除旧的 memoryItems/workflowList 内联列表
10. 端到端验证：滚动加载、calendar 过滤、点击跳转
