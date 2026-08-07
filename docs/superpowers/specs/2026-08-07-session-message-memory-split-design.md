# Session / Message / Memory 架构重构设计

> 状态：Draft（待 review）
> 日期：2026-08-07
> 范围：把 Session 升格为一等实体，拆分消息持久化（SQLite）与向量索引（ChromaDB），由 intent 层决定哪些消息进入向量检索

## 背景与动机

### 核心矛盾

Porto 当前的 `MemoryStore`（`memory/store.py`）是一个"全能"类——它的 `add()` 方法同时做两件事：

1. 写 SQLite `memories` 表（会话历史、列表聚合）
2. 调 embedding 写 ChromaDB 向量库（RAG 检索源）

这把两个本应独立的关注点绑死了。后果是 DIRECT（闲聊）路径来回反复：

| 提交 | 行为 | 问题 |
|---|---|---|
| `cd3b672` fix: persist DIRECT messages | chitchat 也写 SQLite + 向量库 | 闲聊内容污染 RAG 向量检索 |
| `b6327db` refactor: direct chat no longer persists | chitchat 完全不写 | 闲聊消息从 session 历史消失，session 不出现在列表 |

根本原因：**Session 和 Message 是两个产品维度，但当前架构没有独立建模 Session，且消息持久化与向量索引被强制耦合**。

### 设计洞察

- 一个 Session 可以有多条 Message（包括闲聊和 RAG 问答混合）
- **不是所有 Message 都要写入 ChromaDB**——只有 RAG 意图的消息才有检索价值
- Intent 层（`intent.py` 的 `DIRECT` / `RAG` / `QUICK_RAG` / `DEEP_RAG`）天然就是"哪些消息进向量库"的决策者
- 当前 Session 是隐式的（靠 `memories` 表聚合），没有独立元数据（标题、状态、创建时间）

## 目标

- Session 升格为 SQLite 一等实体（`sessions` 表），独立于消息存在
- 所有消息（含 chitchat）都写入 SQLite，确保 session 列表和历史完整
- 只有 RAG 意图的消息才写入 ChromaDB 向量库，避免检索污染
- Intent 层成为向量索引的决策点（langchain 按 intent 分流，agent_sdk 按实际工具调用判定）
- Session 标题由 LLM 异步生成，前端展示更有意义
- 大胆重构，不兼容旧数据

## 非目标（YAGNI）

| 不做 | 理由 |
|---|---|
| 跨 session 全局向量检索 | KB collection 已全局共享知识；session 内聚焦一个话题，跨 session 检索引入噪音 |
| Session 归档/删除 API | 当前只需 active 状态，archived/deleted 延后 |
| 前端显式"新建会话"按钮 | 懒创建已满足需求，不过度设计 |
| 旧数据迁移 | 用户确认不兼容旧代码，DB 重建 |
| Message 级别独立 intent 决策 | 按整轮（turn）决策，user + assistant 共享同一 intent，简单一致 |
| 完整领域模型层（Repository / UoW / 领域事件） | Porto 规模不需要，三个职责清晰的类已足够 |

## 设计决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| Session 生命周期 | 一等实体（`sessions` 表） | 独立于消息存在，有元数据，chitchat-only session 也可见 |
| 向量索引决策粒度 | 按整轮（turn） | 一轮对话是语义单元，拆开决策增加复杂度无收益 |
| Session 标题 | LLM 异步生成（fire-and-forget） | Porto 场景下准确标题体验更好，成本极低 |
| 向量检索范围 | 仅当前 session | 话题隔离，KB 已覆盖全局知识需求 |
| Agent SDK 向量判定 | 事后判定（检查实际工具调用） | 比 pre-classification 更准确，符合 Claude 自主决策哲学 |
| 架构方案 | 拆分双 Store + 编排层 | 职责单一可独立测试，不过度抽象 |

## 架构总览

```
SessionStore (SQLite)              ConversationMemory (ChromaDB)
├─ sessions 表（一等实体）          ├─ index(records) → 向量写入
├─ messages 表（全部消息）          ├─ search(query, session_id) → 检索
├─ session_summaries 表（compaction）├─ count(session_id?) → 调试
├─ session_facts 表（facts）        └─ reset() → 重建
├─ ensure / get / list / update
├─ add_message / list_messages
└─ get_messages_ordered(indexed_only)

         共享函数
         ├── persist_turn(sessions, memory, ...) → 写消息 + 条件索引
         └── maybe_generate_title(sessions, llm, ...) → 异步标题

              ↗ langchain 路径：ChatOrchestrator（intent → store 决策）
ChatOrchestrator
              ↘ agent_sdk 路径：on_stop hook（工具调用 → store 决策）
```

## 1. 数据模型

### `sessions` 表（新增）

```sql
CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,        -- session_id（前端传入）
    title           TEXT,                    -- LLM 异步生成的标题（NULL=未生成）
    status          TEXT DEFAULT 'active',   -- active / archived
    created_at      TEXT NOT NULL,
    last_active_at  TEXT NOT NULL            -- 每条消息写入时 touch
);
```

Session 成为一等实体。`list_sessions` 直接查这张表，不再从消息聚合。`title` 初始 NULL，首轮流结束后异步 LLM 生成。

### `messages` 表（替代 `memories`）

```sql
CREATE TABLE messages (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    role        TEXT NOT NULL,           -- user / assistant
    content     TEXT NOT NULL,
    intent      TEXT,                    -- direct / rag / quick_rag / deep_rag
    indexed     INTEGER DEFAULT 0,       -- 1=已写入 ChromaDB，0=仅 SQLite
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_messages_session ON messages(session_id);
```

关键设计：
- `intent` 列记录消息所属意图（整轮 user+assistant 共享同一 intent）
- `indexed` 标记是否已进向量库——compaction 用 `WHERE indexed=1` 过滤，chitchat 自动排除
- 表名 `memories` → `messages`，语义准确

### ChromaDB collection

继续用现有 memory collection，但**只写 `indexed=1` 的消息**。检索仍按 `where={"session_id": ...}` session 隔离。

### 保留的表

- `session_summaries` — compaction 缓存，不变
- `session_facts` — facts 提取，不变

### 废弃

- `memories` 表 — 被 `messages` 替代
- 旧数据不迁移，DB 重建

## 2. SessionStore（SQLite 层）

纯 SQLite 操作，不碰 ChromaDB。

### 数据类

```python
@dataclass
class Session:
    id: str
    title: str | None
    status: str
    created_at: str
    last_active_at: str

@dataclass
class MessageRecord:
    id: str
    session_id: str
    role: str          # user / assistant
    content: str
    intent: str | None # direct / rag / quick_rag / deep_rag
    indexed: bool
    created_at: str
```

### 接口

```python
class SessionStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._init_db()

    # ── Session ──
    def ensure_session(self, session_id: str) -> Session:
        """懒创建：不存在则 INSERT，返回 Session。每条消息写入前调。"""

    def get_session(self, session_id: str) -> Session | None

    def list_sessions(self, date: str | None = None, limit: int = 20,
                      offset: int = 0) -> tuple[list[dict], int]:
        """查 sessions 表 + LEFT JOIN messages 聚合 count/preview。
        返回的 item 含 title（sessions 表）、message_count、preview（最后一条消息）。"""

    def update_title(self, session_id: str, title: str) -> None:
        """异步 LLM 标题生成后调用。"""

    def touch_session(self, session_id: str) -> None:
        """更新 last_active_at。"""

    # ── Message ──
    def add_message(self, *, session_id: str, role: str, content: str,
                    intent: str | None, indexed: bool = False) -> MessageRecord:
        """写 messages 表。内部先 ensure_session + touch last_active_at。"""

    def list_messages(self, session_id: str, limit: int = 50) -> list[MessageRecord]:
        """倒序（新→旧），供前端历史展示。返回全部消息（含 chitchat）。"""

    def get_messages_ordered(self, session_id: str, *,
                             indexed_only: bool = False,
                             limit: int = 500) -> list[MessageRecord]:
        """正序（旧→新），供 compaction。indexed_only=True 时只返回向量库中的消息。"""

    def mark_indexed(self, message_ids: list[str]) -> None:
        """向量索引成功后回填 indexed flag。"""

    # ── Compaction 缓存 ──
    def get_summary(self, session_id: str) -> SessionSummary | None
    def save_summary(self, session_id: str, summary: str, last_message_id: str) -> None
```

### 关键设计点

1. `ensure_session` 是 `add_message` 的内部前置——调用方不需要关心 session 是否存在
2. `list_sessions` 以 sessions 表为主表——`title` 直接取，`message_count` / `preview` 通过 LEFT JOIN messages 聚合
3. `get_messages_ordered(indexed_only=True)` — compaction 专用，自动排除 chitchat
4. 不再有 `search()` — 向量检索移到 ConversationMemory

## 3. ConversationMemory（ChromaDB 层）

纯向量操作，不碰 SQLite。

### 接口

```python
class ConversationMemory:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.embeddings = EmbeddingClient(settings)
        self.client = chromadb.PersistentClient(path=str(settings.chroma_dir))
        self.collection = self.client.get_or_create_collection(settings.memory_collection)

    def index(self, records: list[MessageRecord]) -> None:
        """批量 embedding + 写入 ChromaDB collection。
        metadata 含 session_id / role / intent / created_at / message_id。
        失败时抛异常，由编排层决定降级策略。"""

    def search(self, query: str, *, session_id: str, top_k: int = 5
               ) -> list[SourceChunk]:
        """session 隔离的向量检索。session_id 必填（API 层面强制约束）。"""

    def count(self, session_id: str | None = None) -> int:
        """向量数（调试/健康检查用）。可选按 session 过滤。"""

    def reset(self) -> None:
        """重建 collection（embedding 维度变化等场景）。"""
```

### 关键设计点

1. `session_id` 在 `search` 中从可选变**必填**——session 隔离是硬约束，API 层面不留全局检索口子
2. `index` 接收 `MessageRecord` 列表——保留 intent/role 等 metadata，检索结果可追溯
3. 不负责更新 `indexed` flag——纯向量操作，不知道 SQLite 的存在

### 现有 MemoryStore 方法去向

| 现有方法 | 去向 |
|---|---|
| `add()` (SQLite + ChromaDB) | 拆分：SessionStore.add_message + ConversationMemory.index |
| `search()` | → ConversationMemory.search |
| `list_session()` | → SessionStore.list_messages |
| `list_sessions()` | → SessionStore.list_sessions |
| `get_messages_ordered()` | → SessionStore.get_messages_ordered |
| `get_summary()` / `save_summary()` | → SessionStore（compaction 缓存仍存 SQLite） |
| `_reset_collection()` | → ConversationMemory.reset |

## 4. ChatOrchestrator + 共享函数（编排层）

### 共享函数

`persist_turn` 和 `maybe_generate_title` 提取为模块级函数。`persist_turn` 用于 DIRECT 路径和 agent_sdk 路径（这两个路径的 user + assistant 消息可以一次性写入）；RAG 路径因为 user 消息必须在 compaction 之后、LLM 之前写入，assistant 消息在 LLM 之后写入，时序不允许一把写，所以 RAG 路径由 orchestrator 直接编排 add_message + index + mark_indexed。`maybe_generate_title` 三条路径共用：

```python
def persist_turn(
    *, sessions: SessionStore, memory: ConversationMemory,
    session_id: str, user_content: str, assistant_content: str,
    intent: str, index_vector: bool,
) -> tuple[MessageRecord, MessageRecord]:
    """写 user + assistant 两条消息。index_vector=True 时额外写向量库 + 回填 indexed flag。"""
    user_msg = sessions.add_message(
        session_id=session_id, role="user",
        content=user_content, intent=intent, indexed=False,
    )
    asst_msg = sessions.add_message(
        session_id=session_id, role="assistant",
        content=assistant_content, intent=intent, indexed=False,
    )
    if index_vector:
        try:
            memory.index([user_msg, asst_msg])
            sessions.mark_indexed([user_msg.id, asst_msg.id])
        except Exception:
            logger.exception("vector index failed session=%s", session_id)
            # 降级：消息已在 SQLite（历史可见），只是不会被向量检索
    return user_msg, asst_msg


def maybe_generate_title(
    sessions: SessionStore, llm: LLMClient, session_id: str, first_message: str,
) -> None:
    """session.title is None 时，异步 fire-and-forget 调 LLM 生成标题。"""
    session = sessions.get_session(session_id)
    if session and session.title is not None:
        return  # 已有标题
    # fire-and-forget（threading.Thread 或 asyncio.create_task，取决于调用上下文）
    ...
```

### ChatOrchestrator（langchain 路径）

```python
class ChatOrchestrator:
    def __init__(self, sessions: SessionStore, memory: ConversationMemory,
                 settings: Settings):
        self.sessions = sessions
        self.memory = memory
        self.settings = settings

    # ── DIRECT 路径 ──
    def handle_direct(self, req, decision, llm) -> ChatResponse:
        """LLM 回答 → persist_turn(index_vector=False) → 标题。"""

    async def handle_direct_stream(self, req, decision, llm) -> AsyncIterator[str]:
        """DIRECT 流式。"""

    # ── RAG 路径 ──
    def handle_rag(self, req, decision, llm) -> ChatResponse:
        """检索 + 回答 + persist_turn(index_vector=True) → 标题。"""

    async def handle_rag_stream(self, req, decision, llm) -> AsyncIterator[str]:
        """RAG 流式。"""
```

### RAG 路径完整流程

```python
def handle_rag(self, req, decision, llm):
    # 1. 检索
    sources = store.search(req.message, top_k=top_k)              # KB 向量库（不变）
    memories = self.memory.search(                                 # 会话向量
        query=req.message, session_id=req.session_id, top_k=5
    )
    summary, recent = get_compacted_history(                       # compaction
        req.session_id, self.sessions, llm                         # indexed_only=True
    )                                                             # 自动排除 chitchat

    # 2. 写 user 消息（SQLite only，暂不索引；compaction 已在步骤 1 跑完，
    #    此处写入是为了 session 历史完整 + 后续轮次的 compaction/facts 能读到）
    user_msg = self.sessions.add_message(
        session_id=req.session_id, role="user",
        content=req.message, intent=decision.intent, indexed=False,
    )

    # 3. Facts + Prompt + LLM 生成（同现有逻辑）
    facts_block = build_facts_prompt(...)
    trigger_facts_extraction_sync(...)
    answer = llm.complete(system_prompt, prompt_parts)

    # 4. 写 assistant 消息 + 索引整轮 + 回填 flag
    asst_msg = self.sessions.add_message(
        session_id=req.session_id, role="assistant",
        content=answer, intent=decision.intent, indexed=False,
    )
    try:
        self.memory.index([user_msg, asst_msg])
        self.sessions.mark_indexed([user_msg.id, asst_msg.id])
    except Exception:
        logger.exception(...)

    # 5. 标题（首轮流后异步生成）
    maybe_generate_title(self.sessions, llm, req.session_id, req.message)

    return ChatResponse(...)
```

> **RAG 路径不用 `persist_turn`**：因为 user 消息（步骤 2）和 assistant 消息（步骤 4）之间隔着 LLM 生成，时序上无法一次性写入。RAG 路径由 orchestrator 直接编排 `add_message` → `index` → `mark_indexed`。`persist_turn` 仅用于 DIRECT 路径和 agent_sdk 路径。

### DIRECT vs RAG 路径对比

| | DIRECT | RAG |
|---|---|---|
| SessionStore.add_message | ✅ (indexed=False) | ✅ (先 False，索引成功后回填) |
| ConversationMemory.search | ❌ | ✅ |
| ConversationMemory.index | ❌ | ✅ |
| Compaction 参与 | ❌ (indexed_only 过滤) | ✅ |
| Facts extraction | ❌ | ✅ |
| KB store.search | ❌ | ✅ |
| Title 生成 | ✅ (首轮流) | ✅ (首轮流) |

### Compaction 改动

`get_compacted_history`（`memory/compaction.py`）签名变更：

```python
# 改前
def get_compacted_history(session_id, store: MemoryStore, llm) -> ...

# 改后
def get_compacted_history(session_id, store: SessionStore, llm) -> ...
```

内部调用 `store.get_messages_ordered(session_id, indexed_only=True)`，chitchat 消息自动排除。

## 5. Agent SDK 路径改造

agent_sdk 不走 ChatOrchestrator（流程是 Claude 自主 MCP tool 调用），但共享 SessionStore + ConversationMemory + 共享函数。改造 `on_stop` hook：

```python
async def on_stop(input_data, tool_use_id, context):
    # 检测本轮是否实际调用了 RAG 工具
    rag_tools = {"search_knowledgebase", "search_memory"}
    used_rag = any(name in rag_tools for (name, _) in _tool_dedup)

    intent = "rag" if used_rag else "direct"

    # 持久化整轮（共享函数）
    persist_turn(
        sessions=session_store,
        memory=conv_memory,
        session_id=req.session_id,
        user_content=req.message,
        assistant_content=state["answer_text"],
        intent=intent,
        index_vector=used_rag,        # 只在实际查库时索引向量
    )

    # 标题（首轮流）
    maybe_generate_title(session_store, llm, req.session_id, req.message)

    # Facts 只在 RAG 轮触发
    if used_rag:
        trigger_facts_extraction_sync(...)
```

## 6. API 层 + 前端 + 依赖注入

### API 路由变更

`memory.py` → `sessions.py`（重命名，语义准确）：

```python
# sessions.py
GET  /api/sessions                          # 不变，但响应含 title
GET  /api/sessions/{session_id}             # 新增：session 详情
GET  /api/sessions/{session_id}/messages    # 原 GET /api/memory/{session_id}
GET  /api/sessions/{session_id}/facts       # 原 GET /api/memory/{session_id}/facts
```

废弃路由：`GET /api/memory/{session_id}`、`GET /api/memory/{session_id}/facts`、`GET /api/memory/search`

`SessionItem` 响应新增 `title`：

```python
class SessionItem(BaseModel):
    session_id: str
    title: str | None       # NEW
    first_at: str
    last_at: str
    message_count: int
    preview: str
```

`POST /api/chat` 和 `POST /api/chat/stream` 路径不变，内部从 MemoryStore 切换到 ChatOrchestrator。

### 依赖注入（`api/deps.py`）

```python
# 新增
def get_session_store() -> SessionStore: ...
def get_conversation_memory() -> ConversationMemory: ...

# 废弃
def get_memory() -> MemoryStore: ...  # 删除
```

### 前端变更

**`session-list.tsx`** — 展示 title：

```tsx
// 优先显示 title，fallback 到 session_id
<span className="truncate font-medium">
  {s.title ?? s.session_id}
</span>
```

**`porto-workbench.tsx` ChatLoader** — API 调用路径改名：

```typescript
// 改前
listMemory(sessionId) → GET /api/memory/{session_id}

// 改后
listMessages(sessionId) → GET /api/sessions/{session_id}/messages
```

返回格式不变，前端历史渲染逻辑不改。`refreshKey` 机制已存在，异步标题生成后会在下次刷新时显示。

**`types.ts`** — `SessionItem` 加 `title`，`MemoryRecord` → `MessageRecord` 加 `intent?`。

## 7. 迁移策略

用户确认"不兼容旧代码"：

1. `_init_db()` 创建新 schema（`sessions` + `messages`），不建 `memories` 表
2. 旧 `memories` / `session_summaries` / `session_facts` 数据不迁移，DB 文件删除重建
3. ChromaDB memory collection：`reset()` 重建（旧向量对应旧 message id，无意义保留）
4. KB collection（知识库向量）不动——与 session/message 无关

## 8. 错误处理与降级

| 故障点 | 行为 | 理由 |
|---|---|---|
| SessionStore 写入失败 | chat 请求抛异常 | 没有会话记录就无法继续 |
| ConversationMemory.index 失败 | 日志告警，`indexed` 保持 False | 消息在 SQLite 可见，只是不参与向量检索——优雅降级 |
| 标题 LLM 调用失败 | title 保持 NULL，前端 fallback 到 session_id | 非关键路径 |
| ChromaDB 维度不匹配 | `ConversationMemory.reset()` 后重试 | 复用现有逻辑 |
| Compaction LLM 失败 | 返回 `("", recent)` 不压缩 | 复用现有降级 |

## 9. 测试策略

| 层 | 测试内容 |
|---|---|
| SessionStore | SQLite CRUD：ensure_session 幂等、add_message 自动创建 session、list_sessions 含 title、get_messages_ordered(indexed_only=True) 过滤 |
| ConversationMemory | index + search 往返、session 隔离（A session 搜不到 B 的向量）、reset 重建 |
| persist_turn | index_vector=False 不写向量 / True 写向量 + 回填 flag / index 失败时 indexed 保持 False |
| ChatOrchestrator | DIRECT 路径不写向量、RAG 路径写向量 + 回填、compaction 只含 indexed 消息 |
| Agent SDK Stop hook | rag_tools 调用 → indexed=True；未调用 → indexed=False |
| 集成 | chitchat 后 session 出现在列表 + 出现在历史、但不出现在向量检索中 |

## 涉及文件清单

### 新增
- `backend/src/porto_chatbot/memory/session_store.py` — SessionStore
- `backend/src/porto_chatbot/memory/conversation_memory.py` — ConversationMemory
- `backend/src/porto_chatbot/memory/persist.py` — persist_turn + maybe_generate_title 共享函数
- `backend/src/porto_chatbot/agent/orchestrator.py` — ChatOrchestrator
- `backend/src/porto_chatbot/api/routes/sessions.py` — sessions API 路由

### 重大修改
- `backend/src/porto_chatbot/memory/store.py` — 废弃，拆分到上述新文件
- `backend/src/porto_chatbot/memory/compaction.py` — 接收 SessionStore，indexed_only 过滤
- `backend/src/porto_chatbot/memory/__init__.py` — 导出新类
- `backend/src/porto_chatbot/agent/langchain_chat.py` — 用 ChatOrchestrator 替代内联 memory 逻辑
- `backend/src/porto_chatbot/agent_sdk/backend.py` — on_stop hook 改用 persist_turn
- `backend/src/porto_chatbot/api/deps.py` — 新增 get_session_store / get_conversation_memory
- `backend/src/porto_chatbot/models/chat.py` — MemoryRecord → MessageRecord（加 intent/indexed）
- `frontend/src/lib/types.ts` — SessionItem 加 title，MemoryRecord → MessageRecord
- `frontend/src/lib/api.ts` — listMemory → listMessages，路径改
- `frontend/src/components/session-list.tsx` — 展示 title
- `frontend/src/components/porto-workbench.tsx` — ChatLoader API 调用改

### 删除
- `backend/src/porto_chatbot/memory/store.py` — 拆分完毕后删除
- `backend/src/porto_chatbot/api/routes/memory.py` — 合入 sessions.py
