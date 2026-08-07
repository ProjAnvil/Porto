# Session / Message / Memory 架构重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 拆分 `MemoryStore` 为 `SessionStore`(SQLite) + `ConversationMemory`(ChromaDB)，Session 升格为一等实体，intent 层控制向量索引——DIRECT(chitchat) 消息只写 SQLite 不进向量库，RAG 消息两者都写。

**Architecture:** 三个职责单一的类 + 编排层：`SessionStore` 管全部 SQLite 操作（sessions/messages/summaries/facts/session_metadata 五张表），`ConversationMemory` 管纯 ChromaDB 向量操作，`ChatOrchestrator` 编排两条 chat 路径。共享函数 `persist_turn` / `index_and_mark` / `maybe_generate_title` 消除重复。

**Tech Stack:** Python 3.12+ / FastAPI / SQLite (WAL mode) / ChromaDB / Pydantic v2 / React + TypeScript (Next.js)

## Global Constraints

- **DB 不兼容旧数据**：新 schema 直接建，旧 `memories` 表不迁移，DB 文件删除重建
- **Python 包管理**：`uv`（`cd backend && uv run pytest` 跑测试，`uv run ruff check` 跑 lint）
- **测试约定**：所有测试走 deterministic degraded 路径（conftest 隔离 LLM env），用 `sample_settings` fixture（tmp_path data_dir）
- **Embedding**：测试用 local deterministic embedding（128 dims），不走真 LLM
- **提交消息格式**：`feat: ...` / `refactor: ...` / `chore: ...`
- **SQLite 连接策略**：SessionStore 使用 per-operation 连接（`with sqlite3.connect(...) as conn`），与旧 MemoryStore 模式一致。每次操作打开新连接，WAL 模式自动启用。这是最安全的并发策略——daemon 线程（标题生成）与请求线程不会共享连接对象。
- **ChromaDB**：memory collection 名 `porto_memory`，KB collection 名 `porto_kb`（后者不动）
- **旧数据全弃**：spec 第 7 节确认不迁移

## Reviewer 反馈决议（implement 前 lock）

| 问题 | 决议 |
|---|---|
| C1 session_metadata 表 | SessionStore._init_db 建表，`get_claude_session`/`save_claude_session` 移入 SessionStore |
| C2 ChatOrchestrator 缺 KB store + dispatch | 加 `kb_store: LocalVectorStore` 参数；`handle()` 做 intent routing + rag_available + dispatch |
| C3 search_memory 工具 session_id 冲突 | AgentToolContext 加 `session_id` 字段，工具优先用 ctx.session_id |
| C4 RAG 不可用分支 | ChatOrchestrator.handle() 内 check，不通过时走 `_handle_rag_unavailable` |
| M1 compaction indexed_only 语义 | 设计决策：chitchat 从 compaction + facts + recent context 中排除（仅从向量索引排除的 spec 描述不够精确，这里明确：indexed_only 过滤影响 compaction 和 facts 读取的 recent_turns） |
| M2 标题生成异步策略 | 统一用 `threading.Thread(daemon=True)` |
| M3 list_sessions preview N+1 | 用关联子查询消除 N+1 |
| M4 SQLite 并发 | per-operation 连接（最安全，无共享连接并发问题） |
| M5 DI 生命周期 | get_session_store / get_conversation_memory 作为 _ensure_rag_singletons 缓存的单例 |
| M6 DIRECT 路径持久化 | 新行为：DIRECT 也写 SQLite（index_vector=False） |
| M7 agent_sdk intent | 二元 `direct`/`rag`（无 quick_rag/deep_rag） |
| M8 models/__init__.py | 更新 re-exports |

---

## File Structure

### 新增文件

| 文件 | 职责 |
|---|---|
| `backend/src/porto_chatbot/memory/session_store.py` | SessionStore：纯 SQLite，5 张表，Session/MessageRecord 数据类 |
| `backend/src/porto_chatbot/memory/conversation_memory.py` | ConversationMemory：纯 ChromaDB 向量操作 |
| `backend/src/porto_chatbot/memory/persist.py` | 共享函数：index_and_mark, persist_turn, maybe_generate_title |
| `backend/src/porto_chatbot/agent/orchestrator.py` | ChatOrchestrator：编排 langchain chat 流程 |
| `backend/src/porto_chatbot/api/routes/sessions.py` | sessions API 路由（替代 memory.py） |
| `backend/tests/memory/test_session_store.py` | SessionStore 单元测试 |
| `backend/tests/memory/test_conversation_memory.py` | ConversationMemory 单元测试 |
| `backend/tests/memory/test_persist.py` | persist_turn / title 测试 |
| `backend/tests/agent/test_orchestrator.py` | ChatOrchestrator 测试 |

### 重大修改

| 文件 | 改动 |
|---|---|
| `backend/src/porto_chatbot/models/chat.py` | MemoryRecord → MessageRecord，加 intent/indexed |
| `backend/src/porto_chatbot/models/__init__.py` | 更新 re-exports |
| `backend/src/porto_chatbot/memory/compaction.py` | 接收 SessionStore，indexed_only=True |
| `backend/src/porto_chatbot/memory/__init__.py` | 导出新类 |
| `backend/src/porto_chatbot/agent/langchain_chat.py` | 用 ChatOrchestrator 替代内联 memory 逻辑 |
| `backend/src/porto_chatbot/agent_sdk/backend.py` | on_stop 用 persist_turn；session_metadata 走 SessionStore |
| `backend/src/porto_chatbot/agent_sdk/tools.py` | search_memory 用 ctx.session_id |
| `backend/src/porto_chatbot/tools/context.py` | AgentToolContext 加 session_id |
| `backend/src/porto_chatbot/api/deps.py` | 加 get_session_store / get_conversation_memory 单例 |
| `backend/src/porto_chatbot/api/routes/memory.py` | 删除，合入 sessions.py |
| `backend/src/porto_chatbot/memory/store.py` | 删除 |
| `backend/src/porto_chatbot/memory/facts.py` | SessionFactsStore 无需改动（仍直接用 memory_db_path） |
| `frontend/src/lib/types.ts` | SessionItem 加 title；MemoryRecord → MessageRecord |
| `frontend/src/lib/api.ts` | listMemory → listMessages；路径改 |
| `frontend/src/components/session-list.tsx` | 展示 title |
| `frontend/src/components/porto-workbench.tsx` | API 调用路径改名 |

---

## Task 1: MessageRecord 数据模型

**Files:**
- Modify: `backend/src/porto_chatbot/models/chat.py:41-47`
- Modify: `backend/src/porto_chatbot/models/__init__.py:3-10,66-128`

**Interfaces:**
- Produces: `MessageRecord(BaseModel)` with fields `id, session_id, role: str, content, intent: str|None, indexed: bool, created_at, metadata: dict`
- Produces: backward-compat alias `MemoryRecord = MessageRecord` (temporary, removed in Task 10)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/memory/test_models.py — 追加到已有文件末尾
from porto_chatbot.models import MessageRecord

def test_message_record_has_intent_and_indexed():
    r = MessageRecord(
        id="m1", session_id="s1", role="user", content="hello",
        created_at="2026-01-01T00:00:00Z",
    )
    assert r.intent is None
    assert r.indexed is False
    assert r.metadata == {}

def test_message_record_with_intent():
    r = MessageRecord(
        id="m2", session_id="s1", role="assistant", content="answer",
        intent="rag", indexed=True, created_at="2026-01-01T00:00:00Z",
    )
    assert r.intent == "rag"
    assert r.indexed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/memory/test_models.py -v`
Expected: FAIL with "cannot import name 'MessageRecord'"

- [ ] **Step 3: Implement MessageRecord**

Replace `MemoryRecord` class in `models/chat.py:41-47` with:

```python
class MessageRecord(BaseModel):
    """单条会话消息——SQLite messages 表的一行。"""
    id: str
    session_id: str
    role: str
    content: str
    intent: str | None = None        # direct / rag / quick_rag / deep_rag
    indexed: bool = False            # True=已写入 ChromaDB 向量库
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)
```

In `models/__init__.py`, update imports and `__all__`:
- Line 7: `"MemoryRecord"` → `"MessageRecord"` (and add `MessageRecord` to import from `.chat`)
- Add backward-compat alias at bottom: `MemoryRecord = MessageRecord  # deprecated alias`
- Keep `__all__` exporting `MessageRecord`; also keep `MemoryRecord` string in `__all__` for compat

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/memory/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Verify nothing else breaks**

Run: `cd backend && uv run pytest tests/ -x -q --ignore=tests/test_langgraph_spike.py --ignore=tests/test_langgraph_orchestration_spike.py 2>&1 | tail -20`
Expected: PASS (backward-compat alias covers existing imports)

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/models/chat.py backend/src/porto_chatbot/models/__init__.py backend/tests/memory/test_models.py
git commit -m "refactor: rename MemoryRecord to MessageRecord with intent/indexed fields"
```

---

## Task 2: SessionStore — SQLite 层

**Files:**
- Create: `backend/src/porto_chatbot/memory/session_store.py`
- Test: `backend/tests/memory/test_session_store.py`

**Interfaces:**
- Consumes: `Settings` (for paths), `MessageRecord` from Task 1
- Produces: `Session` dataclass, `SessionStore` class with:
  - `ensure_session(session_id) -> Session`
  - `get_session(session_id) -> Session | None`
  - `list_sessions(date=None, limit=20, offset=0) -> tuple[list[dict], int]`
  - `update_title(session_id, title) -> None`
  - `touch_session(session_id) -> None`
  - `add_message(*, session_id, role, content, intent=None, indexed=False) -> MessageRecord`
  - `list_messages(session_id, limit=50) -> list[MessageRecord]`
  - `get_messages_ordered(session_id, *, indexed_only=False, limit=500) -> list[MessageRecord]`
  - `mark_indexed(message_ids: list[str]) -> None`
  - `get_summary(session_id) -> SessionSummary | None`
  - `save_summary(session_id, summary, last_message_id) -> None`
  - `get_claude_session(porto_session_id) -> str | None`
  - `save_claude_session(porto_session_id, claude_session_id) -> None`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/memory/test_session_store.py
"""SessionStore 单元测试——纯 SQLite CRUD。"""
from porto_chatbot.memory.session_store import Session, SessionStore, SessionSummary


def test_ensure_session_is_idempotent(sample_settings):
    store = SessionStore(sample_settings)
    s1 = store.ensure_session("s1")
    assert s1.id == "s1"
    assert s1.status == "active"
    assert s1.title is None
    s2 = store.ensure_session("s1")
    assert s2.id == "s1"
    # Should not duplicate
    store2 = SessionStore(sample_settings)
    s3 = store2.ensure_session("s1")
    assert s3.id == "s1"


def test_add_message_creates_session_and_touches(sample_settings):
    store = SessionStore(sample_settings)
    msg = store.add_message(
        session_id="auto", role="user", content="hello", intent="direct",
    )
    assert msg.session_id == "auto"
    assert msg.intent == "direct"
    assert msg.indexed is False
    session = store.get_session("auto")
    assert session is not None
    assert session.last_active_at >= session.created_at


def test_list_messages_returns_all_desc(sample_settings):
    store = SessionStore(sample_settings)
    store.add_message(session_id="s1", role="user", content="first", intent="direct")
    store.add_message(session_id="s1", role="assistant", content="second", intent="direct")
    msgs = store.list_messages("s1")
    assert len(msgs) == 2
    assert msgs[0].content == "second"  # DESC (new→old)
    assert msgs[1].content == "first"


def test_get_messages_ordered_asc(sample_settings):
    store = SessionStore(sample_settings)
    store.add_message(session_id="s1", role="user", content="first", intent="rag", indexed=False)
    store.add_message(session_id="s1", role="assistant", content="second", intent="rag", indexed=False)
    store.mark_indexed([m.id for m in store.list_messages("s1")])
    store.add_message(session_id="s1", role="user", content="chitchat", intent="direct", indexed=False)
    # All messages
    all_msgs = store.get_messages_ordered("s1")
    assert len(all_msgs) == 3
    assert all_msgs[0].content == "first"  # ASC
    # indexed_only filters out chitchat
    indexed = store.get_messages_ordered("s1", indexed_only=True)
    assert len(indexed) == 2
    assert all(m.indexed for m in indexed)


def test_list_sessions_with_title_and_preview(sample_settings):
    store = SessionStore(sample_settings)
    store.update_title("s1", "My Chat")
    store.add_message(session_id="s1", role="user", content="hello world this is a long preview message", intent="direct")
    items, total = store.list_sessions()
    assert total == 1
    assert items[0]["session_id"] == "s1"
    assert items[0]["title"] == "My Chat"
    assert items[0]["message_count"] == 1
    assert "hello world" in items[0]["preview"]
    assert items[0]["first_at"] is not None
    assert items[0]["last_at"] is not None


def test_claude_session_mapping(sample_settings):
    store = SessionStore(sample_settings)
    assert store.get_claude_session("p1") is None
    store.save_claude_session("p1", "claude-abc")
    assert store.get_claude_session("p1") == "claude-abc"
    store.save_claude_session("p1", "claude-xyz")  # update
    assert store.get_claude_session("p1") == "claude-xyz"


def test_summary_cache(sample_settings):
    store = SessionStore(sample_settings)
    assert store.get_summary("s1") is None
    store.save_summary("s1", "summary text", "msg-99")
    s = store.get_summary("s1")
    assert isinstance(s, SessionSummary)
    assert s.summary == "summary text"
    assert s.last_message_id == "msg-99"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/memory/test_session_store.py -v`
Expected: FAIL with "No module named 'porto_chatbot.memory.session_store'"

- [ ] **Step 3: Implement SessionStore**

Create `backend/src/porto_chatbot/memory/session_store.py`:

```python
"""SessionStore — 纯 SQLite 操作层。

管理 5 张表：sessions, messages, session_summaries, session_facts, session_metadata。
所有 chat 路径的消息持久化都经过这里；ChromaDB 向量操作在 ConversationMemory 中。
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from ..logging_utils import get_component_logger
from ..models import MessageRecord
from ..settings import Settings

_DEFAULT_MESSAGE_FETCH_LIMIT = 500


@dataclass
class Session:
    """会话一等实体。"""
    id: str
    title: str | None
    status: str
    created_at: str
    last_active_at: str


@dataclass
class SessionSummary:
    """缓存的会话历史摘要（compaction 命中缓存时复用）。"""
    summary: str
    last_message_id: str
    created_at: str


class SessionStore:
    """SQLite 层：sessions + messages + summaries + facts + claude session mapping。

    使用 per-operation 连接（每次操作打开新连接），与旧 MemoryStore 一致。
    WAL 模式自动启用，安全支持 daemon 线程（标题生成等）并发写。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("session_store", settings)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.logger.info("session store ready db=%s", settings.memory_db_path)

    def _conn(self):
        """打开一个新连接（per-operation），启用 WAL + FK。"""
        conn = sqlite3.connect(str(self.settings.memory_db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── 内部 ──

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id              TEXT PRIMARY KEY,
                    title           TEXT,
                    status          TEXT DEFAULT 'active',
                    created_at      TEXT NOT NULL,
                    last_active_at  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id          TEXT PRIMARY KEY,
                    session_id  TEXT NOT NULL REFERENCES sessions(id),
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    intent      TEXT,
                    indexed     INTEGER DEFAULT 0,
                    created_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
                CREATE TABLE IF NOT EXISTS session_summaries (
                    session_id      TEXT PRIMARY KEY,
                    summary         TEXT NOT NULL,
                    last_message_id TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_facts (
                    id          TEXT PRIMARY KEY,
                    session_id  TEXT NOT NULL,
                    category    TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'active',
                    source_msg_id TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_facts_session ON session_facts(session_id);
                CREATE INDEX IF NOT EXISTS idx_facts_session_cat ON session_facts(session_id, category);
                CREATE TABLE IF NOT EXISTS session_metadata (
                    session_id        TEXT PRIMARY KEY,
                    claude_session_id TEXT,
                    updated_at        TEXT
                );
            """)
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    # ── Session ──

    def ensure_session(self, session_id: str) -> Session:
        """懒创建：不存在则 INSERT。per-operation 连接 = 线程安全。"""
        now = self._now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, title, status, created_at, last_active_at FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sessions (id, title, status, created_at, last_active_at) "
                    "VALUES (?, NULL, 'active', ?, ?)",
                    (session_id, now, now),
                )
                conn.commit()
                return Session(id=session_id, title=None, status="active",
                               created_at=now, last_active_at=now)
            return Session(id=row[0], title=row[1], status=row[2],
                           created_at=row[3], last_active_at=row[4])

    def get_session(self, session_id: str) -> Session | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, title, status, created_at, last_active_at FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return Session(id=row[0], title=row[1], status=row[2],
                       created_at=row[3], last_active_at=row[4])

    def list_sessions(
        self, date: str | None = None, limit: int = 20, offset: int = 0,
    ) -> tuple[list[dict], int]:
        """查 sessions 表，LEFT JOIN messages 聚合 count + preview。无 N+1。"""
        params: list[object] = []
        date_clause = ""
        if date:
            date_clause = "WHERE substr(s.last_active_at, 1, 10) = ?"
            params.append(date)
        with self._conn() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM sessions s {date_clause}", params,
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT s.id, s.title, s.created_at AS first_at,
                       s.last_active_at AS last_at,
                       COUNT(m.id) AS message_count,
                       (SELECT m2.content FROM messages m2
                        WHERE m2.session_id = s.id
                        ORDER BY m2.created_at DESC LIMIT 1) AS preview
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id
                {date_clause}
                GROUP BY s.id
                ORDER BY s.last_active_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        items = [
            {
                "session_id": r[0],
                "title": r[1],
                "first_at": r[2],
                "last_at": r[3],
                "message_count": r[4],
                "preview": (r[5] or "")[:80],
            }
            for r in rows
        ]
        self.logger.info(
            "sessions list date=%s limit=%s offset=%s total=%s", date, limit, offset, total,
        )
        return items, total

    def update_title(self, session_id: str, title: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET title=? WHERE id=?", (title, session_id),
            )
            conn.commit()

    def touch_session(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET last_active_at=? WHERE id=?",
                (self._now(), session_id),
            )
            conn.commit()

    # ── Message ──

    def add_message(
        self, *, session_id: str, role: str, content: str,
        intent: str | None = None, indexed: bool = False,
    ) -> MessageRecord:
        """写 messages 表。内部先 ensure_session + touch。"""
        self.ensure_session(session_id)
        msg = MessageRecord(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role=role,
            content=content,
            intent=intent,
            indexed=indexed,
            created_at=self._now(),
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, intent, indexed, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg.id, msg.session_id, msg.role, msg.content, msg.intent,
                 int(msg.indexed), msg.created_at),
            )
            conn.execute(
                "UPDATE sessions SET last_active_at=? WHERE id=?",
                (msg.created_at, session_id),
            )
            conn.commit()
        self.logger.info(
            "message added id=%s session=%s role=%s intent=%s chars=%s",
            msg.id, msg.session_id, msg.role, msg.intent, len(msg.content),
        )
        return msg

    def list_messages(self, session_id: str, limit: int = 50) -> list[MessageRecord]:
        """倒序（新→旧），供前端历史展示。返回全部消息（含 chitchat）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, session_id, role, content, intent, indexed, created_at "
                "FROM messages WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._row_to_msg(r) for r in rows]

    def get_messages_ordered(
        self, session_id: str, *, indexed_only: bool = False,
        limit: int = _DEFAULT_MESSAGE_FETCH_LIMIT,
    ) -> list[MessageRecord]:
        """正序（旧→新），供 compaction。indexed_only=True 时只返回向量库中的消息。"""
        clause = "AND indexed=1 " if indexed_only else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT id, session_id, role, content, intent, indexed, created_at "
                f"FROM messages WHERE session_id=? {clause}"
                f"ORDER BY created_at ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._row_to_msg(r) for r in rows]

    def mark_indexed(self, message_ids: list[str]) -> None:
        if not message_ids:
            return
        with self._conn() as conn:
            conn.executemany(
                "UPDATE messages SET indexed=1 WHERE id=?",
                [(mid,) for mid in message_ids],
            )
            conn.commit()

    @staticmethod
    def _row_to_msg(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            id=row[0], session_id=row[1], role=row[2], content=row[3],
            intent=row[4], bool(row[5]), row[6],
        )

    # ── Compaction 缓存 ──

    def get_summary(self, session_id: str) -> SessionSummary | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT summary, last_message_id, created_at FROM session_summaries WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return SessionSummary(summary=row[0], last_message_id=row[1], created_at=row[2])

    def save_summary(self, session_id: str, summary: str, last_message_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO session_summaries (session_id, summary, last_message_id, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "  summary=excluded.summary, last_message_id=excluded.last_message_id, "
                "  created_at=excluded.created_at",
                (session_id, summary, last_message_id, self._now()),
            )
            conn.commit()

    # ── Claude session mapping（agent_sdk resume） ──

    def get_claude_session(self, porto_session_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT claude_session_id FROM session_metadata WHERE session_id=?",
                (porto_session_id,),
            ).fetchone()
        return row[0] if row else None

    def save_claude_session(self, porto_session_id: str, claude_session_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO session_metadata (session_id, claude_session_id, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "  claude_session_id=excluded.claude_session_id, "
                "  updated_at=excluded.updated_at",
                (porto_session_id, claude_session_id, self._now()),
            )
            conn.commit()

    def close(self) -> None:
        """per-operation 模式下无共享连接需关闭，此方法为 no-op（接口兼容）。"""
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/memory/test_session_store.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/memory/session_store.py backend/tests/memory/test_session_store.py
git commit -m "feat: add SessionStore — SQLite layer with sessions/messages/summaries/facts/metadata tables"
```

---

## Task 3: ConversationMemory — ChromaDB 层

**Files:**
- Create: `backend/src/porto_chatbot/memory/conversation_memory.py`
- Test: `backend/tests/memory/test_conversation_memory.py`

**Interfaces:**
- Consumes: `Settings`, `EmbeddingClient`, `MessageRecord` from Task 1
- Produces: `ConversationMemory` class with `index(records)`, `search(query, *, session_id, top_k=5)`, `count(session_id=None)`, `reset()`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/memory/test_conversation_memory.py
"""ConversationMemory 单元测试——纯 ChromaDB 向量操作。"""
from porto_chatbot.memory.conversation_memory import ConversationMemory
from porto_chatbot.models import MessageRecord


def _msg(session_id="s1", role="user", content="hello", intent="rag"):
    return MessageRecord(
        id=f"m-{content}", session_id=session_id, role=role, content=content,
        intent=intent, created_at="2026-01-01T00:00:00Z",
    )


def test_index_and_search_roundtrip(sample_settings):
    mem = ConversationMemory(sample_settings)
    mem.index([_msg(content="payment platform architecture")])
    # Search with the same content — local embeddings guarantee self-match
    results = mem.search("payment platform architecture", session_id="s1", top_k=5)
    assert len(results) >= 1
    assert "payment" in results[0].text.lower()


def test_session_isolation(sample_settings):
    mem = ConversationMemory(sample_settings)
    mem.index([_msg(session_id="A", content="alpha topic")])
    mem.index([_msg(session_id="B", content="beta topic")])
    results_a = mem.search("alpha", session_id="A")
    results_b = mem.search("alpha", session_id="B")
    assert any("alpha" in r.text.lower() for r in results_a)
    assert not any("alpha" in r.text.lower() for r in results_b)


def test_count(sample_settings):
    mem = ConversationMemory(sample_settings)
    assert mem.count() == 0
    mem.index([_msg(content="x"), _msg(content="y")])
    assert mem.count() == 2
    assert mem.count(session_id="s1") == 2
    assert mem.count(session_id="other") == 0


def test_reset(sample_settings):
    mem = ConversationMemory(sample_settings)
    mem.index([_msg(content="x")])
    assert mem.count() == 1
    mem.reset()
    assert mem.count() == 0


def test_empty_collection_search_returns_empty(sample_settings):
    mem = ConversationMemory(sample_settings)
    results = mem.search("anything", session_id="s1")
    assert results == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/memory/test_conversation_memory.py -v`
Expected: FAIL with "No module named 'porto_chatbot.memory.conversation_memory'"

- [ ] **Step 3: Implement ConversationMemory**

Create `backend/src/porto_chatbot/memory/conversation_memory.py`:

```python
"""ConversationMemory — 纯 ChromaDB 向量操作层。

只做 index/search/count/reset，不碰 SQLite。session_id 在 search 中必填。
"""
from __future__ import annotations

import chromadb

from ..embeddings import EmbeddingClient
from ..logging_utils import get_component_logger
from ..models import MessageRecord, SourceChunk
from ..settings import Settings


class ConversationMemory:
    """ChromaDB 层：会话向量的索引与检索。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("conv_memory", settings)
        self.settings.chroma_dir.mkdir(parents=True, exist_ok=True)
        self.embeddings = EmbeddingClient(settings)
        self.client = chromadb.PersistentClient(path=str(settings.chroma_dir))
        self.collection = self.client.get_or_create_collection(settings.memory_collection)
        self.logger.info(
            "conversation memory ready collection=%s", settings.memory_collection,
        )

    def index(self, records: list[MessageRecord]) -> None:
        """批量 embedding + 写入 ChromaDB。metadata 含 session_id/role/intent/created_at/message_id。

        失败时抛异常（维度不匹配则自动 reset 重试），由调用方决定降级策略。
        """
        if not records:
            return
        embeddings = self.embeddings.embed_documents([r.content for r in records])
        try:
            self.collection.add(
                ids=[r.id for r in records],
                documents=[r.content for r in records],
                metadatas=[
                    {
                        "session_id": r.session_id,
                        "role": r.role,
                        "intent": r.intent or "",
                        "created_at": r.created_at,
                        "message_id": r.id,
                    }
                    for r in records
                ],
                embeddings=embeddings,
            )
        except Exception as exc:
            if "dimension" not in str(exc).lower():
                raise
            self.logger.warning("memory collection dim mismatch on index, rebuilding: %s", exc)
            self.reset()
            self.collection.add(
                ids=[r.id for r in records],
                documents=[r.content for r in records],
                metadatas=[
                    {"session_id": r.session_id, "role": r.role, "intent": r.intent or "",
                     "created_at": r.created_at, "message_id": r.id}
                    for r in records
                ],
                embeddings=embeddings,
            )
        self.logger.info("memory indexed records=%s", len(records))

    def search(
        self, query: str, *, session_id: str, top_k: int = 5,
    ) -> list[SourceChunk]:
        """session 隔离的向量检索。session_id 必填。"""
        if self.collection.count() == 0:
            return []
        query_embedding = self.embeddings.embed_query(query)
        try:
            result = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                where={"session_id": session_id},
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            if "dimension" not in str(exc).lower():
                raise
            self.logger.warning("memory collection dim mismatch on search, rebuilding: %s", exc)
            self.reset()
            return []
        rows: list[SourceChunk] = []
        for item_id, doc, metadata, distance in zip(
            result.get("ids", [[]])[0],
            result.get("documents", [[]])[0],
            result.get("metadatas", [[]])[0],
            result.get("distances", [[]])[0],
            strict=False,
        ):
            rows.append(
                SourceChunk(
                    id=item_id,
                    path=f"memory:{metadata.get('session_id', '')}",
                    title=str(metadata.get("role", "memory")),
                    text=doc or "",
                    score=round(1.0 / (1.0 + max(0.0, float(distance))), 4),
                    metadata=dict(metadata),
                )
            )
        self.logger.info(
            "memory search session=%s query_chars=%s results=%s",
            session_id, len(query), len(rows),
        )
        return rows

    def count(self, session_id: str | None = None) -> int:
        """向量数。可选按 session 过滤。"""
        if session_id is None:
            return self.collection.count()
        # ChromaDB count with where filter
        result = self.collection.get(where={"session_id": session_id})
        return len(result.get("ids", []))

    def reset(self) -> None:
        """重建 collection（embedding 维度变化等场景）。注意：会清空向量记忆。"""
        try:
            self.client.delete_collection(self.settings.memory_collection)
        except Exception:
            self.logger.info("memory collection reset skipped (not existed)")
        self.collection = self.client.get_or_create_collection(self.settings.memory_collection)
        self.logger.info("memory collection reset done")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/memory/test_conversation_memory.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/memory/conversation_memory.py backend/tests/memory/test_conversation_memory.py
git commit -m "feat: add ConversationMemory — ChromaDB layer with session-isolated vector search"
```

---

## Task 4: 共享函数 (persist.py) + memory/__init__.py

**Files:**
- Create: `backend/src/porto_chatbot/memory/persist.py`
- Modify: `backend/src/porto_chatbot/memory/__init__.py`
- Test: `backend/tests/memory/test_persist.py`

**Interfaces:**
- Consumes: `SessionStore` from Task 2, `ConversationMemory` from Task 3, `LLMClient`
- Produces: `index_and_mark(sessions, memory, records) -> None`, `persist_turn(...)`, `maybe_generate_title(...)`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/memory/test_persist.py
"""persist_turn / index_and_mark / maybe_generate_title 测试。"""
from unittest.mock import MagicMock, patch

from porto_chatbot.memory.conversation_memory import ConversationMemory
from porto_chatbot.memory.persist import (
    index_and_mark,
    maybe_generate_title,
    persist_turn,
)
from porto_chatbot.memory.session_store import SessionStore


def test_persist_turn_no_index(sample_settings):
    sessions = SessionStore(sample_settings)
    memory = ConversationMemory(sample_settings)
    user_msg, asst_msg = persist_turn(
        sessions=sessions, memory=memory, session_id="s1",
        user_content="hi", assistant_content="hello",
        intent="direct", index_vector=False,
    )
    assert user_msg.role == "user"
    assert asst_msg.role == "assistant"
    assert user_msg.indexed is False
    assert memory.count() == 0  # not indexed


def test_persist_turn_with_index(sample_settings):
    sessions = SessionStore(sample_settings)
    memory = ConversationMemory(sample_settings)
    user_msg, asst_msg = persist_turn(
        sessions=sessions, memory=memory, session_id="s1",
        user_content="what is payment", assistant_content="payment service",
        intent="rag", index_vector=True,
    )
    assert user_msg.indexed is True
    assert asst_msg.indexed is True
    assert memory.count() == 2


def test_persist_turn_index_failure_graceful(sample_settings):
    sessions = SessionStore(sample_settings)
    memory = ConversationMemory(sample_settings)
    # Force index to raise
    with patch.object(memory, "index", side_effect=RuntimeError("boom")):
        user_msg, asst_msg = persist_turn(
            sessions=sessions, memory=memory, session_id="s1",
            user_content="q", assistant_content="a",
            intent="rag", index_vector=True,
        )
    # Messages persisted to SQLite, but indexed stays False
    assert user_msg.indexed is False
    assert asst_msg.indexed is False
    msgs = sessions.list_messages("s1")
    assert len(msgs) == 2


def test_maybe_generate_title_skips_if_title_exists(sample_settings):
    sessions = SessionStore(sample_settings)
    sessions.ensure_session("s1")  # Must create session first
    sessions.update_title("s1", "Existing")
    llm = MagicMock()
    maybe_generate_title(sessions, llm, "s1", "first message")
    llm.complete.assert_not_called()


def test_maybe_generate_title_generates_for_new_session(sample_settings):
    sessions = SessionStore(sample_settings)
    sessions.add_message(session_id="s1", role="user", content="hello", intent="direct")
    llm = MagicMock()
    llm.complete.return_value = "Generated Title"
    maybe_generate_title(sessions, llm, "s1", "hello")
    # Thread is fire-and-forget; wait a moment for it
    import time
    time.sleep(0.5)
    session = sessions.get_session("s1")
    assert session.title == "Generated Title"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/memory/test_persist.py -v`
Expected: FAIL with "No module named 'porto_chatbot.memory.persist'"

- [ ] **Step 3: Implement persist.py**

Create `backend/src/porto_chatbot/memory/persist.py`:

```python
"""共享编排函数——消除 DIRECT/agent_sdk/RAG 路径的消息持久化重复。

persist_turn：写 user + assistant 两条消息，可选索引向量。
index_and_mark：批量索引 + 回填 indexed flag（persist_turn 和 RAG 路径共用）。
maybe_generate_title：异步 fire-and-forget 生成 session 标题。
"""
from __future__ import annotations

import threading

from ..logging_utils import get_component_logger
from ..models import MessageRecord
from .conversation_memory import ConversationMemory
from .session_store import SessionStore

logger = get_component_logger("persist")


def index_and_mark(
    sessions: SessionStore, memory: ConversationMemory, records: list[MessageRecord],
) -> None:
    """批量 embedding + 写 ChromaDB + 回填 indexed flag。

    index 失败时只记日志——消息已在 SQLite（历史可见），只是不会被向量检索（优雅降级）。
    ChromaDB batch add 是原子的：如果它抛异常，两条消息都不会被写入向量库，
    所以跳过 mark_indexed 不会让 flag 与 ChromaDB 状态不同步。
    """
    if not records:
        return
    try:
        memory.index(records)
        sessions.mark_indexed([r.id for r in records])
    except Exception:
        logger.exception(
            "vector index failed records=%s", [r.id for r in records],
        )


def persist_turn(
    *,
    sessions: SessionStore,
    memory: ConversationMemory,
    session_id: str,
    user_content: str,
    assistant_content: str,
    intent: str,
    index_vector: bool,
) -> tuple[MessageRecord, MessageRecord]:
    """写 user + assistant 两条消息。index_vector=True 时额外写向量库 + 回填 flag。

    用于 DIRECT 路径和 agent_sdk 路径（这两条路径的 user+assistant 可以一次性写入）。
    RAG 路径不用此函数——user 消息在 LLM 之前写，assistant 在之后写，时序不同。
    """
    user_msg = sessions.add_message(
        session_id=session_id, role="user",
        content=user_content, intent=intent, indexed=False,
    )
    asst_msg = sessions.add_message(
        session_id=session_id, role="assistant",
        content=assistant_content, intent=intent, indexed=False,
    )
    if index_vector:
        index_and_mark(sessions, memory, [user_msg, asst_msg])
    return user_msg, asst_msg


def _generate_title_thread(
    sessions: SessionStore, llm, session_id: str, first_message: str,
) -> None:
    """在 daemon thread 中调 LLM 生成标题并写入 sessions 表。"""
    try:
        title = llm.complete(
            "你是会话标题生成器。用 10-15 个中文字概括以下用户消息的主题，只输出标题，不要标点。",
            f"用户消息:\n{first_message}",
        )
        title = (title or "").strip()[:50]
        if title:
            sessions.update_title(session_id, title)
            logger.info("title generated session=%s title=%s", session_id, title)
    except Exception:
        logger.exception("title generation failed session=%s", session_id)


def maybe_generate_title(
    sessions: SessionStore, llm, session_id: str, first_message: str,
) -> None:
    """session.title is None 时，fire-and-forget（daemon thread）调 LLM 生成标题。

    使用 threading.Thread(daemon=True)——在 sync 和 async 上下文中都安全。
    TOCTOU 竞争（两个并发请求都生成标题）可接受：update_title 是幂等的。
    """
    session = sessions.get_session(session_id)
    if session and session.title is not None:
        return
    if not llm or not getattr(llm, "enabled", False):
        return
    t = threading.Thread(
        target=_generate_title_thread,
        args=(sessions, llm, session_id, first_message),
        daemon=True,
    )
    t.start()
```

Update `memory/__init__.py`:

```python
from .compaction import get_compacted_history, summarize_records
from .conversation_memory import ConversationMemory
from .facts import (
    SessionFactsStore,
    build_facts_prompt,
    extract_facts,
    trigger_facts_extraction_async,
    trigger_facts_extraction_sync,
)
from .persist import index_and_mark, maybe_generate_title, persist_turn
from .session_store import Session, SessionStore, SessionSummary
from .store import MemoryStore  # 临时保留，Task 10 删除

__all__ = [
    # 旧（临时）
    "MemoryStore",
    # 新
    "SessionStore",
    "Session",
    "SessionSummary",
    "ConversationMemory",
    "persist_turn",
    "index_and_mark",
    "maybe_generate_title",
    # 不变
    "SessionFactsStore",
    "build_facts_prompt",
    "extract_facts",
    "get_compacted_history",
    "summarize_records",
    "trigger_facts_extraction_async",
    "trigger_facts_extraction_sync",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/memory/test_persist.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/memory/persist.py backend/src/porto_chatbot/memory/__init__.py backend/tests/memory/test_persist.py
git commit -m "feat: add persist_turn / index_and_mark / maybe_generate_title shared helpers"
```

---

## Task 5: Compaction 更新

**Files:**
- Modify: `backend/src/porto_chatbot/memory/compaction.py`
- Modify: `backend/tests/memory/test_compaction.py`

**Interfaces:**
- Consumes: `SessionStore` from Task 2 (replaces `MemoryStore`)
- Produces: `get_compacted_history(session_id, store: SessionStore, llm, ...)` with `indexed_only=True` semantics

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/memory/test_compaction.py`:

```python
from porto_chatbot.memory.session_store import SessionStore


def test_compaction_only_uses_indexed_messages(sample_settings):
    """indexed_only=True: chitchat (indexed=False) 被排除出 compaction。"""
    store = SessionStore(sample_settings)
    # Add 25 indexed messages (above threshold=20)
    for i in range(25):
        msg = store.add_message(
            session_id="s1", role="user" if i % 2 == 0 else "assistant",
            content=f"rag message {i}", intent="rag", indexed=False,
        )
    store.mark_indexed([m.id for m in store.list_messages("s1")])
    # Add 5 chitchat messages (not indexed)
    for i in range(5):
        store.add_message(
            session_id="s1", role="user",
            content=f"chitchat {i}", intent="direct", indexed=False,
        )
    summary, recent = get_compacted_history("s1", store, llm=None)
    # LLM disabled → returns ("", recent). recent should only contain indexed messages.
    # Without LLM, threshold logic still runs: total indexed = 25 > 20 → keep_recent
    assert len(recent) <= store.settings.memory_recent_keep
    assert all(r.indexed for r in recent)  # no chitchat in recent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/memory/test_compaction.py::test_compaction_only_uses_indexed_messages -v`
Expected: FAIL (get_compacted_history still expects MemoryStore)

- [ ] **Step 3: Update compaction.py**

Replace `compaction.py` imports and `get_compacted_history`:

```python
# Line 9-10: change imports
from ..llm import LLMClient
from ..models import MessageRecord
from .session_store import SessionStore
```

In `get_compacted_history`, change:
- `store: MemoryStore` → `store: SessionStore`
- `records = store.get_messages_ordered(session_id)` → `records = store.get_messages_ordered(session_id, indexed_only=True)`
- `store.logger` → use module-level logger instead (import `get_component_logger`)

Full updated function:

```python
import logging

logger = logging.getLogger(__name__)

# ... summarize_records stays the same but change MemoryRecord type hint to MessageRecord ...


def get_compacted_history(
    session_id: str,
    store: SessionStore,
    llm: LLMClient | None,
    *,
    keep_recent: int | None = None,
    threshold: int | None = None,
) -> tuple[str, list[MessageRecord]]:
    """返回 (历史摘要, 近期原文消息)。只处理 indexed=True 的消息（chitchat 自动排除）。

    - 消息数 ≤ 阈值：返回 ("", 全部 indexed 消息)，不压缩。
    - 消息数 > 阈值：旧消息摘要压缩（带缓存），近期 keep_recent 条保留原文。
    - LLM 不可用：不压缩，降级返回 ("", 近期 keep_recent 条)。
    """
    settings = store.settings
    keep = keep_recent if keep_recent is not None else settings.memory_recent_keep
    thresh = threshold if threshold is not None else settings.memory_compact_threshold

    records = store.get_messages_ordered(session_id, indexed_only=True)
    if len(records) <= thresh:
        return "", records

    old = records[:-keep] if keep > 0 else records
    recent = records[-keep:] if keep > 0 else []

    if not llm or not llm.enabled:
        logger.info("memory compaction skipped (llm disabled) session_id=%s", session_id)
        return "", recent

    last_old_id = old[-1].id if old else None
    cached = store.get_summary(session_id)
    if cached and cached.last_message_id == last_old_id and cached.summary:
        logger.info(
            "memory compaction cache hit session_id=%s last_message_id=%s",
            session_id, last_old_id,
        )
        return cached.summary, recent

    summary = summarize_records(old, llm)
    if summary and last_old_id:
        store.save_summary(session_id, summary, last_old_id)
    logger.info(
        "memory compaction done session_id=%s old=%s recent=%s summary_chars=%s",
        session_id, len(old), len(recent), len(summary),
    )
    return summary, recent
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/memory/ -v`
Expected: PASS (all memory tests)

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/memory/compaction.py backend/tests/memory/test_compaction.py
git commit -m "refactor: compaction uses SessionStore with indexed_only filter (chitchat excluded)"
```

---

## Task 6: ChatOrchestrator + langchain_chat 重构

**Files:**
- Create: `backend/src/porto_chatbot/agent/orchestrator.py`
- Modify: `backend/src/porto_chatbot/agent/langchain_chat.py`
- Test: `backend/tests/agent/test_orchestrator.py`

**Interfaces:**
- Consumes: `SessionStore` (T2), `ConversationMemory` (T3), `LocalVectorStore`, `Settings`
- Produces: `ChatOrchestrator(sessions, memory, kb_store, settings)` with `handle(req)`, `handle_stream(req)`

**这是最高风险的 Task。** `langchain_chat.py` 有 596 行（含 sync + stream + SSE 协议 + 评估 + 步骤构建）。ChatOrchestrator 提取核心 chat 逻辑，langchain_chat.py 变成薄 wrapper。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/agent/test_orchestrator.py
"""ChatOrchestrator 测试——验证 DIRECT/RAG 路径的持久化行为。"""
import pytest
from unittest.mock import MagicMock, patch

from porto_chatbot.agent.orchestrator import ChatOrchestrator
from porto_chatbot.memory.conversation_memory import ConversationMemory
from porto_chatbot.memory.session_store import SessionStore
from porto_chatbot.models import ChatRequest
from porto_chatbot.models.enums import ChatIntent
from porto_chatbot.vector_store import LocalVectorStore


@pytest.fixture
def orch(sample_settings):
    sessions = SessionStore(sample_settings)
    memory = ConversationMemory(sample_settings)
    kb_store = LocalVectorStore(sample_settings)
    return ChatOrchestrator(sessions, memory, kb_store, sample_settings)


def test_direct_path_persists_sqlite_not_vector(orch, sample_settings):
    """DIRECT 路径：写 SQLite 不写向量库。"""
    req = ChatRequest(message="你好", session_id="test-direct")
    with patch.object(orch, "_route_intent", return_value=MagicMock(
            intent=ChatIntent.DIRECT, reason="greeting")), \
         patch.object(orch, "_llm_complete", return_value="你好！"):
        resp = orch.handle(req)
    msgs = orch.sessions.list_messages("test-direct")
    assert len(msgs) == 2  # user + assistant
    assert orch.memory.count() == 0  # not indexed


def test_rag_path_persists_both(orch, sample_settings):
    """RAG 路径：写 SQLite + 写向量库 + 回填 indexed flag。"""
    req = ChatRequest(message="what is payment", session_id="test-rag")
    with patch.object(orch, "_route_intent", return_value=MagicMock(
            intent=ChatIntent.RAG, reason="keyword")), \
         patch.object(orch, "_check_rag_available", return_value=(True, None)), \
         patch.object(orch, "_llm_complete", return_value="payment service handles it"):
        resp = orch.handle(req)
    msgs = orch.sessions.list_messages("test-rag")
    assert len(msgs) == 2
    assert all(m.indexed for m in msgs)
    assert orch.memory.count() == 2


def test_chitchat_excluded_from_vector_search(orch, sample_settings):
    """先 chitchat，再 RAG——chitchat 不出现在向量检索结果中。"""
    # Turn 1: chitchat
    req1 = ChatRequest(message="你好", session_id="s1")
    with patch.object(orch, "_route_intent", return_value=MagicMock(
            intent=ChatIntent.DIRECT, reason="greeting")), \
         patch.object(orch, "_llm_complete", return_value="你好！"):
        orch.handle(req1)
    # Turn 2: RAG
    req2 = ChatRequest(message="payment", session_id="s1")
    with patch.object(orch, "_route_intent", return_value=MagicMock(
            intent=ChatIntent.RAG, reason="keyword")), \
         patch.object(orch, "_check_rag_available", return_value=(True, None)), \
         patch.object(orch, "_llm_complete", return_value="payment info"):
        orch.handle(req2)
    # Vector search should find payment, not 你好
    results = orch.memory.search("payment", session_id="s1")
    texts = " ".join(r.text.lower() for r in results)
    assert "payment" in texts
    assert "你好" not in texts
    # But session history shows both
    all_msgs = orch.sessions.list_messages("s1")
    assert len(all_msgs) == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/agent/test_orchestrator.py -v`
Expected: FAIL with "No module named 'porto_chatbot.agent.orchestrator'"

- [ ] **Step 3: Implement ChatOrchestrator**

Create `backend/src/porto_chatbot/agent/orchestrator.py`:

```python
"""ChatOrchestrator — langchain chat 路径的编排层。

从 langchain_chat.py 提取核心 chat 逻辑：intent routing → RAG availability →
retrieval → compaction → facts → prompt → LLM → 持久化 → 评估。

用 SessionStore + ConversationMemory 替代旧 MemoryStore，实现：
- DIRECT 消息写 SQLite 不写向量库
- RAG 消息写 SQLite + 写向量库 + 回填 indexed flag
- Session 一等实体（自动 ensure + 标题生成）
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ..api.deps import effective_rag_chat_settings, get_index_supervisor
from ..api.sse import _ai_sdk_sse, _text_chunks
from ..evaluation import evaluate_rag_cases
from ..intent import IntentDecision, route_chat_intent
from ..llm import LLMClient, format_sources
from ..logging_utils import get_component_logger
from ..memory import (
    SessionFactsStore,
    build_facts_prompt,
    get_compacted_history,
    index_and_mark,
    maybe_generate_title,
    persist_turn,
    trigger_facts_extraction_async,
    trigger_facts_extraction_sync,
)
from ..models import ChatRequest, ChatResponse, EvalCase, MessageRecord
from ..models.enums import ChatIntent, IntentRoutingMode, QueryTransformStrategy
from ..query_transform import retrieve_with_transform
from ..vector_store import LocalVectorStore

logger = get_component_logger("orchestrator")

_SOURCE_PREVIEW_CHARS = 180
_MAX_FALLBACK_SOURCES = 4
_MAX_SSE_SOURCES = 6


def _trim_to_budget(parts: list[str], budget: int) -> list[str]:
    """超字符预算时从后向前截断：保留问题/摘要/会话，裁剪 memories/sources。"""
    if budget <= 0 or sum(len(p) for p in parts) <= budget:
        return parts
    suffix = "…（已截断）"
    result = list(parts)
    for i in range(len(result) - 1, -1, -1):
        total = sum(len(p) for p in result)
        if total <= budget:
            break
        over = total - budget
        part = result[i]
        keep = len(part) - over
        if keep <= 0:
            result[i] = ""
        else:
            room = keep - len(suffix)
            result[i] = (part[:room] + suffix) if room > 0 else part[:keep]
    return [p for p in result if p]


class ChatOrchestrator:
    """编排 langchain chat 流程。

    sync: handle() → ChatResponse
    async: handle_stream() → AsyncIterator[str] (SSE)
    """

    def __init__(
        self,
        sessions,  # SessionStore
        memory,    # ConversationMemory
        kb_store: LocalVectorStore,
        settings,
    ):
        self.sessions = sessions
        self.memory = memory
        self.kb_store = kb_store
        self.settings = settings

    # ── 路由辅助 ──

    def _route_intent(self, req: ChatRequest, llm: LLMClient) -> IntentDecision:
        rag_chat = effective_rag_chat_settings()
        routing_mode = rag_chat.intent_routing_mode or IntentRoutingMode.BINARY
        if routing_mode == IntentRoutingMode.OFF:
            return IntentDecision(ChatIntent.RAG, "routing_off")
        return route_chat_intent(req.message, self.settings, llm, routing_mode=routing_mode)

    @property
    def _transform_strategy(self) -> QueryTransformStrategy:
        rag_chat = effective_rag_chat_settings()
        return rag_chat.query_transform_strategy or QueryTransformStrategy.NONE

    def _check_rag_available(self) -> tuple[bool, str | None]:
        return get_index_supervisor().rag_available()

    def _llm_complete(self, system: str, user: str, llm: LLMClient | None = None) -> str:
        llm = llm or LLMClient(self.settings)
        return llm.complete(system, user)

    # ── 主入口 ──

    def handle(self, req: ChatRequest) -> ChatResponse:
        """sync chat 入口——intent routing → dispatch → persist → evaluate。"""
        logger.info(
            "chat start session=%s chars=%s", req.session_id, len(req.message),
        )
        llm = LLMClient(self.settings)
        decision = self._route_intent(req, llm)

        if decision.intent == ChatIntent.DIRECT:
            return self._handle_direct(req, decision, llm)

        available, reason = self._check_rag_available()
        if not available:
            return self._handle_rag_unavailable(req, reason, decision, llm)

        return self._handle_rag(req, decision, llm)

    # ── DIRECT 路径 ──

    def _handle_direct(
        self, req: ChatRequest, decision: IntentDecision, llm: LLMClient,
    ) -> ChatResponse:
        answer = self._llm_complete(
            "你是 Porto 助手。用户当前消息不需要检索知识库，直接、简洁、友好地回应。",
            f"用户消息:\n{req.message}",
            llm,
        )
        if not answer:
            if decision.reason == "greeting":
                answer = "你好！我是 Porto 助手，可以帮你查询知识库、拆解 PRD，或生成子系统需求。"
            elif decision.reason == "smalltalk_or_help":
                answer = "我是 Porto 助手，可以进行知识库问答、PRD 分析和子系统拆分。"
            else:
                answer = "我在。你可以继续提问，或说明需要查询哪部分知识库内容。"

        persist_turn(
            sessions=self.sessions, memory=self.memory, session_id=req.session_id,
            user_content=req.message, assistant_content=answer,
            intent="direct", index_vector=False,
        )
        maybe_generate_title(self.sessions, llm, req.session_id, req.message)

        logger.info("chat direct finish session=%s reason=%s", req.session_id, decision.reason)
        return ChatResponse(
            answer=answer, sources=[], memory=[],
            evaluation={"score": 0.0, "passed": True, "cases": []},
            steps=[
                {"name": "route_intent", "status": "completed",
                 "summary": f"direct: {decision.reason}",
                 "data": {"intent": decision.intent, "reason": decision.reason}},
                {"name": "answer", "status": "completed",
                 "summary": "直接回复，未调用 RAG", "data": {}},
            ],
        )

    # ── RAG 不可用 ──

    _RAG_UNAVAILABLE_HINTS = {
        "reindexing": "知识库正在重建索引，请等待完成后再提问。",
        "index_unavailable": "知识库索引不可用，请在设置中触发重新索引后再提问。",
    }

    def _handle_rag_unavailable(
        self, req: ChatRequest, reason: str | None,
        decision: IntentDecision, llm: LLMClient,
    ) -> ChatResponse:
        hint = self._RAG_UNAVAILABLE_HINTS.get(reason or "", "知识库当前不可用，请稍后重试。")
        # Persist user message + hint as a turn (index_vector=False)
        persist_turn(
            sessions=self.sessions, memory=self.memory, session_id=req.session_id,
            user_content=req.message, assistant_content=hint,
            intent="rag", index_vector=False,
        )
        maybe_generate_title(self.sessions, llm, req.session_id, req.message)
        return ChatResponse(
            answer=hint, sources=[], memory=[],
            evaluation={"score": 0.0, "passed": False, "cases": []},
            steps=[
                {"name": "route_intent", "status": "completed",
                 "summary": f"rag unavailable: {reason}",
                 "data": {"reason": reason}},
                {"name": "retrieve_knowledge", "status": "completed",
                 "summary": hint, "data": {}},
                {"name": "answer", "status": "completed",
                 "summary": "RAG 不可用，返回提示", "data": {}},
            ],
        )

    # ── RAG 路径 ──

    def _handle_rag(
        self, req: ChatRequest, decision: IntentDecision, llm: LLMClient,
    ) -> ChatResponse:
        top_k = self.settings.top_k
        transform_strategy = self._transform_strategy
        transform_degraded: str | None = None

        self.kb_store.ensure_index()
        if decision.intent == ChatIntent.QUICK_RAG:
            sources = self.kb_store.search(req.message, top_k=top_k)
        else:
            result = retrieve_with_transform(
                req.message, transform_strategy, self.kb_store, self.settings, llm, top_k,
            )
            sources = result.chunks
            transform_degraded = result.degrade_reason if result.degraded else None

        memories = self.memory.search(req.message, session_id=req.session_id, top_k=5)
        summary, recent = get_compacted_history(req.session_id, self.sessions, llm)

        # Write user message BEFORE LLM (for session history + future compaction)
        user_msg = self.sessions.add_message(
            session_id=req.session_id, role="user",
            content=req.message, intent=str(decision.intent), indexed=False,
        )

        # Facts
        facts_store = SessionFactsStore(self.settings)
        facts_block = ""
        if self.settings.facts_enabled:
            try:
                facts_block = build_facts_prompt(facts_store.by_category(req.session_id))
            except Exception:
                logger.exception("facts load failed session=%s", req.session_id)
        trigger_facts_extraction_sync(
            store=facts_store, llm=llm, session_id=req.session_id,
            new_message=req.message, recent_turns=recent, settings=self.settings,
        )

        # Prompt assembly
        prompt_parts = [f"用户问题:\n{req.message}"]
        if facts_block:
            prompt_parts.append(facts_block)
        if summary:
            prompt_parts.append(f"会话历史摘要:\n{summary}")
        prompt_parts.append("最近会话:\n" + "\n".join(f"{m.role}: {m.content}" for m in recent))
        prompt_parts.append(f"记忆检索:\n{format_sources(memories)}")
        prompt_parts.append(f"知识库片段:\n{format_sources(sources)}")
        prompt_parts = _trim_to_budget(prompt_parts, self.settings.context_char_budget)

        answer = self._llm_complete(
            "你是 Porto 知识库问答助手。优先基于知识库片段回答，也可引用会话记忆；不确定时说明缺口。",
            "\n\n".join(prompt_parts), llm,
        )
        if not answer:
            if sources:
                bullets = "\n".join(
                    f"- [{i + 1}] {s.path}: {s.text[:_SOURCE_PREVIEW_CHARS].replace(chr(10), ' ')}"
                    for i, s in enumerate(sources[:_MAX_FALLBACK_SOURCES])
                )
                answer = f"我在知识库中找到以下相关内容：\n{bullets}\n\n建议优先查看匹配分最高的文档。"
            else:
                answer = "当前知识库没有检索到相关片段。请先执行知识库索引。"

        # Write assistant message + index + mark
        asst_msg = self.sessions.add_message(
            session_id=req.session_id, role="assistant",
            content=answer, intent=str(decision.intent), indexed=False,
        )
        index_and_mark(self.sessions, self.memory, [user_msg, asst_msg])
        maybe_generate_title(self.sessions, llm, req.session_id, req.message)

        evaluation = evaluate_rag_cases([
            EvalCase(question=req.message, answer=answer,
                     contexts=[s.text for s in sources]),
        ]).model_dump()

        logger.info(
            "chat rag finish session=%s sources=%s memories=%s score=%s",
            req.session_id, len(sources), len(memories), evaluation["score"],
        )
        return ChatResponse(
            answer=answer, sources=sources, memory=memories,
            evaluation=evaluation, transform_degraded=transform_degraded,
            steps=[
                {"name": "route_intent", "status": "completed",
                 "summary": f"rag: {decision.reason}",
                 "data": {"intent": decision.intent, "reason": decision.reason}},
                {"name": "retrieve_memory", "status": "completed",
                 "summary": f"检索到 {len(memories)} 条记忆，近期 {len(recent)} 条"
                            + ("（含历史摘要）" if summary else ""),
                 "data": {"compacted": bool(summary), "recent": len(recent),
                          "memory_hits": len(memories)}},
                {"name": "retrieve_knowledge", "status": "completed",
                 "summary": f"检索到 {len(sources)} 个片段", "data": {}},
                {"name": "answer", "status": "completed",
                 "summary": "完成回答生成", "data": {}},
                {"name": "evaluate_rag", "status": "completed",
                 "summary": f"RAG eval score {evaluation['score']}", "data": evaluation},
            ],
        )

    # ── 流式入口 ──

    async def handle_stream(self, req: ChatRequest) -> AsyncIterator[str]:
        """async chat 流式入口——yield SSE chunks。

        保留 langchain_chat_stream 的 SSE 协议。
        """
        # 委托给模块级流式函数（从 langchain_chat.py 搬入）
        from .langchain_chat_stream import stream_chat

        async for chunk in stream_chat(self, req):
            yield chunk
```

> **注意**：流式 SSE 逻辑较复杂（约 250 行），从 `langchain_chat.py` 的 `langchain_chat_stream` + 子生成器搬入新文件 `langchain_chat_stream.py`，将 `memory.add` / `memory.search` / `get_compacted_history` 调用改为 orchestrator 的 `sessions`/`memory` 属性。流式 DIRECT 路径在流结束后调 `persist_turn(index_vector=False)`，RAG 路径在流前写 user msg、流后写 assistant + index。

Then modify `langchain_chat.py` to be a thin wrapper:

```python
# backend/src/porto_chatbot/agent/langchain_chat.py
"""Langchain chatbot entry points — thin wrappers around ChatOrchestrator."""
from __future__ import annotations

from collections.abc import AsyncIterator

from ..api.deps import get_conversation_memory, get_session_store, get_store
from ..models import ChatRequest, ChatResponse
from .orchestrator import ChatOrchestrator


def langchain_chat(req: ChatRequest, settings) -> ChatResponse:
    """Langchain chatbot 同步入口。"""
    sessions = get_session_store(settings)
    memory = get_conversation_memory(settings)
    kb_store = get_store(settings)
    orch = ChatOrchestrator(sessions, memory, kb_store, settings)
    return orch.handle(req)


async def langchain_chat_stream(req: ChatRequest, settings) -> AsyncIterator[str]:
    """Langchain chatbot 流式入口。"""
    sessions = get_session_store(settings)
    memory = get_conversation_memory(settings)
    kb_store = get_store(settings)
    orch = ChatOrchestrator(sessions, memory, kb_store, settings)
    async for chunk in orch.handle_stream(req):
        yield chunk
```

> **流式实现细节**：创建 `agent/langchain_chat_stream.py`，包含 `stream_chat(orch, req)` 异步生成器。其内部子生成器 `_stream_direct_path`, `_stream_rag_path`, `_stream_rag_unavailable_path` 从原 `langchain_chat.py` 搬入，修改点：
> - `memory.add(role="user", ...)` → `orch.sessions.add_message(role="user", intent=..., indexed=False)`
> - `memory.search(...)` → `orch.memory.search(..., session_id=...)`
> - `get_compacted_history(session_id, memory, llm)` → `get_compacted_history(session_id, orch.sessions, llm)`
> - DIRECT 路径：流结束后 `persist_turn(sessions=orch.sessions, memory=orch.memory, ..., index_vector=False)`
> - RAG 路径：流前写 user msg，流后写 assistant msg + `index_and_mark(orch.sessions, orch.memory, [user_msg, asst_msg])`
> - SSE 事件格式 (`_ai_sdk_sse`, `_text_chunks`) 完全不变

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/agent/test_orchestrator.py -v`
Expected: PASS (all 3 tests)

- [ ] **Step 5: Run existing chat tests to verify no regression**

Run: `cd backend && uv run pytest tests/test_chat_dispatch.py tests/test_agent.py tests/test_api.py -x -q 2>&1 | tail -20`
Expected: PASS (may need minor adjustments for mock setup)

- [ ] **Step 6: Commit**

```bash
git add backend/src/porto_chatbot/agent/orchestrator.py \
       backend/src/porto_chatbot/agent/langchain_chat.py \
       backend/src/porto_chatbot/agent/langchain_chat_stream.py \
       backend/tests/agent/test_orchestrator.py
git commit -m "feat: ChatOrchestrator replaces inline memory logic — DIRECT no-index, RAG index+mark"
```

---

## Task 7: Agent SDK 路径改造

**Files:**
- Modify: `backend/src/porto_chatbot/agent_sdk/backend.py:109-148,315-493`
- Modify: `backend/src/porto_chatbot/tools/context.py:36-47`
- Modify: `backend/src/porto_chatbot/agent_sdk/tools.py:184-209`

**Interfaces:**
- Consumes: `SessionStore` (T2), `ConversationMemory` (T3), `persist_turn` (T4)
- Produces: Updated `on_stop` hook using `persist_turn`, `AgentToolContext.session_id`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_agent_sdk_backend.py — 追加
import pytest
from unittest.mock import MagicMock, patch
from collections import Counter


def test_decide_intent_from_tool_calls_with_rag():
    """RAG 工具调用 → intent='rag', index_vector=True。"""
    from porto_chatbot.agent_sdk.backend import decide_intent_from_tool_calls
    tool_calls: Counter[tuple[str, str]] = Counter()
    tool_calls[("search_knowledgebase", '{"query":"payment"}')] += 1
    intent, index_vector = decide_intent_from_tool_calls(tool_calls)
    assert intent == "rag"
    assert index_vector is True


def test_decide_intent_from_tool_calls_without_rag():
    """无 RAG 工具调用 → intent='direct', index_vector=False。"""
    from porto_chatbot.agent_sdk.backend import decide_intent_from_tool_calls
    tool_calls: Counter[tuple[str, str]] = Counter()
    tool_calls[("get_prd_text", '{}')] += 1
    intent, index_vector = decide_intent_from_tool_calls(tool_calls)
    assert intent == "direct"
    assert index_vector is False


def test_decide_intent_search_memory_counts_as_rag():
    """search_memory 也算 RAG 工具。"""
    from porto_chatbot.agent_sdk.backend import decide_intent_from_tool_calls
    tool_calls: Counter[tuple[str, str]] = Counter()
    tool_calls[("search_memory", '{"query":"history"}')] += 1
    intent, _ = decide_intent_from_tool_calls(tool_calls)
    assert intent == "rag"


def test_agent_tool_context_has_session_id():
    from porto_chatbot.tools.context import AgentToolContext
    ctx = AgentToolContext(state={}, session_id="test-sid")
    assert ctx.session_id == "test-sid"
    # Default is None for workflow mode
    ctx2 = AgentToolContext(state={})
    assert ctx2.session_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_agent_sdk_backend.py -k "test_agent_tool_context_has_session_id" -v`
Expected: FAIL (AgentToolContext has no session_id)

- [ ] **Step 3: Update AgentToolContext**

In `tools/context.py:36-47`, add `session_id`:

```python
@dataclass
class AgentToolContext:
    """工具执行上下文：持有可变的 workflow state 与向量库句柄。

    chatbot 模式额外传 memory_store / facts_store / session_id（workflow 模式为 None）。
    """

    state: State
    vector_store: LocalVectorStore | None = None
    memory_store: Any = None       # chatbot 专属（ConversationMemory），workflow 为 None
    facts_store: Any = None        # chatbot 专属（SessionFactsStore），workflow 为 None
    file_service: Any = None       # FileService（files/service.py），未注入时为 None
    session_id: str | None = None  # chatbot 专属（当前会话 id），供 search_memory 工具使用
```

- [ ] **Step 4: Update search_memory tool**

In `agent_sdk/tools.py:184-209`, change `search_mem` to use `ctx.session_id`:

```python
    if ctx.memory_store is not None:
        @tool(
            "search_memory",
            "跨会话语义检索对话记忆。自动限定到当前会话范围。",
            {"query": str},
        )
        async def search_mem(args):  # noqa: ANN001
            # 优先用 ctx.session_id（chatbot 模式注入），不依赖 Claude 传参
            sid = ctx.session_id
            if not sid:
                return _mcp_text("错误：未设置 session_id，无法执行记忆检索。")
            try:
                results = await asyncio.wait_for(
                    asyncio.to_thread(
                        ctx.memory_store.search,
                        str(args.get("query", "")),
                        session_id=sid,
                    ),
                    timeout=tool_timeout,
                )
            except TimeoutError:
                return _mcp_text("记忆检索超时，请重试。")
            from ..tools.context import _format_chunks

            ctx.state.setdefault("tool_memory", []).extend(results)
            return _mcp_text(
                _format_chunks(results) if results else "无匹配记忆。"
            )

        tools.append(search_mem)
```

- [ ] **Step 5: Update `_build_chat_options` and `on_stop` in backend.py**

Add module-level pure function `decide_intent_from_tool_calls`:

```python
# Add near top of backend.py, after imports
_RAG_TOOL_NAMES = {"search_knowledgebase", "search_memory"}


def decide_intent_from_tool_calls(
    tool_dedup: Counter[tuple[str, str]],
) -> tuple[str, bool]:
    """根据本轮实际工具调用决定 intent 和是否索引向量。

    纯函数，可独立单元测试。检查 Counter 的 keys（tool_name, input_json）中
    是否包含 RAG 工具名。
    """
    used_rag = any(name in _RAG_TOOL_NAMES for (name, _) in tool_dedup)
    intent = "rag" if used_rag else "direct"
    return intent, used_rag
```

Replace the top of `_build_chat_options` (lines 332-352) to use new deps:

```python
        from ..api.deps import (
            get_file_service, get_conversation_memory, get_session_store, get_store,
        )
        from ..llm import LLMClient
        from ..memory import SessionFactsStore, persist_turn, maybe_generate_title
        from ..memory.persist import index_and_mark

        kb_store = get_store(settings)
        sessions = get_session_store(settings)
        conv_memory = get_conversation_memory(settings)
        facts_store = SessionFactsStore(settings)
        kb_store.ensure_index()

        ctx = AgentToolContext(
            state={},
            vector_store=kb_store,
            memory_store=conv_memory,  # ConversationMemory (has .search with required session_id)
            facts_store=facts_store,
            file_service=file_service,
            session_id=req.session_id,  # NEW: inject for search_memory tool
        )
```

Replace the `on_stop` hook (lines 363-391):

```python
        async def on_stop(input_data, tool_use_id, context):  # noqa: ANN001
            """Stop hook: persist user+assistant turn, then trigger facts extraction.

            检测本轮是否实际调用了 RAG 工具（search_knowledgebase / search_memory），
            只在实际查库时索引向量。Fail-open。
            """
            try:
                intent, used_rag = decide_intent_from_tool_calls(_tool_dedup)

                persist_turn(
                    sessions=sessions, memory=conv_memory,
                    session_id=req.session_id,
                    user_content=req.message,
                    assistant_content=state["answer_text"],
                    intent=intent,
                    index_vector=used_rag,
                )
                maybe_generate_title(sessions, LLMClient(settings), req.session_id, req.message)

                if used_rag:
                    trigger_facts_extraction_sync(
                        store=facts_store, llm=LLMClient(settings),
                        session_id=req.session_id, new_message=req.message,
                        recent_turns=[], settings=settings,
                    )
            except Exception:
                self.logger.exception("stop hook failed session=%s", req.session_id)
            return {}
```

Update `_get_claude_session` / `_set_claude_session` calls (lines 485, 309) to use SessionStore:

```python
        # In _build_chat_options, replace _get_claude_session call:
        from ..api.deps import get_session_store
        sessions_for_resume = get_session_store(settings)
        existing_claude_sid = sessions_for_resume.get_claude_session(req.session_id)

        # In _capture_session, replace _set_claude_session call:
        sessions_for_resume.save_claude_session(porto_sid, returned_sid)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_agent_sdk_backend.py tests/test_sdk_tools.py -v -x 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/src/porto_chatbot/tools/context.py \
       backend/src/porto_chatbot/agent_sdk/tools.py \
       backend/src/porto_chatbot/agent_sdk/backend.py \
       backend/tests/test_agent_sdk_backend.py
git commit -m "refactor: agent_sdk on_stop uses persist_turn, search_memory uses ctx.session_id"
```

---

## Task 8: API 路由 + 依赖注入

**Files:**
- Create: `backend/src/porto_chatbot/api/routes/sessions.py`
- Modify: `backend/src/porto_chatbot/api/deps.py`
- Delete: `backend/src/porto_chatbot/api/routes/memory.py`

**Interfaces:**
- Consumes: `SessionStore` (T2), `ConversationMemory` (T3)
- Produces: `get_session_store()`, `get_conversation_memory()` singleton factories; `/api/sessions*` routes

- [ ] **Step 1: Add singleton factories to deps.py**

In `deps.py`, add after `get_memory` (line 218):

```python
def get_session_store(runtime_settings=None) -> SessionStore:
    """按 data_dir 缓存的 SessionStore 单例（挂入 _ensure_rag_singletons）。"""
    from ..memory import SessionStore

    entry = _ensure_rag_singletons()
    key = "session_store"
    store = entry.get(key)
    if store is None:
        store = SessionStore(runtime_settings or current_settings())
        entry[key] = store
    return store


def get_conversation_memory(runtime_settings=None) -> ConversationMemory:
    """按 data_dir 缓存的 ConversationMemory 单例（挂入 _ensure_rag_singletons）。"""
    from ..memory import ConversationMemory

    entry = _ensure_rag_singletons()
    key = "conv_memory"
    mem = entry.get(key)
    if mem is None:
        mem = ConversationMemory(runtime_settings or current_settings())
        entry[key] = mem
    return mem
```

Update `reset_rag_singletons` to close SessionStore connection:

```python
def reset_rag_singletons() -> None:
    global _rag_singletons
    for entry in _rag_singletons.values():
        # ... existing supervisor/health/checkpoint cleanup ...
        try:
            ss = entry.get("session_store")
            if ss is not None:
                ss.close()
        except Exception as exc:
            logger.warning("session store close failed: %s", exc)
    _rag_singletons = {}
```

- [ ] **Step 2: Create sessions.py route**

Create `backend/src/porto_chatbot/api/routes/sessions.py`:

```python
"""Session / Message API routes。

从 memory.py 演化而来：sessions 是一等实体，messages 是子资源。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..deps import current_settings, get_session_store
from ...models import MessageRecord, SessionFact

router = APIRouter(prefix="/api", tags=["sessions"])


class SessionItem(BaseModel):
    session_id: str
    title: str | None = None
    first_at: str
    last_at: str
    message_count: int
    preview: str


class SessionListResponse(BaseModel):
    items: list[SessionItem]
    total: int
    has_more: bool


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(date: str | None = None, limit: int = 20, offset: int = 0):
    items, total = get_session_store().list_sessions(date=date, limit=limit, offset=offset)
    return SessionListResponse(
        items=[SessionItem(**item) for item in items],
        total=total,
        has_more=(offset + limit) < total,
    )


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    store = get_session_store()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    return {
        "session_id": session.id,
        "title": session.title,
        "status": session.status,
        "created_at": session.created_at,
        "last_active_at": session.last_active_at,
    }


@router.get("/sessions/{session_id}/messages")
def list_messages(session_id: str, limit: int = 50):
    store = get_session_store()
    items = store.list_messages(session_id, limit=limit)
    return {"session_id": session_id, "items": items}


@router.get("/sessions/{session_id}/facts")
def get_session_facts(session_id: str):
    from ...memory import SessionFactsStore

    store = SessionFactsStore(current_settings())
    grouped = store.by_category(session_id)
    # Flatten grouped dict to list
    facts: list[SessionFact] = []
    for cat_facts in grouped.values():
        facts.extend(cat_facts)
    return {"session_id": session_id, "facts": facts}
```

- [ ] **Step 3: Register new router, unregister old**

In `main.py` or wherever routers are registered:

```python
# Replace memory router with sessions router
from .api.routes.sessions import router as sessions_router
app.include_router(sessions_router)
# Remove: from .api.routes.memory import router as memory_router
```

- [ ] **Step 4: Run existing API tests**

Run: `cd backend && uv run pytest tests/test_api.py tests/api/ -x -q 2>&1 | tail -20`
Expected: PASS (update test imports if needed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/porto_chatbot/api/routes/sessions.py \
       backend/src/porto_chatbot/api/deps.py \
       backend/src/porto_chatbot/main.py
git rm backend/src/porto_chatbot/api/routes/memory.py
git commit -m "feat: sessions API routes replace memory.py; add get_session_store/get_conversation_memory singletons"
```

---

## Task 9: 前端适配

**Files:**
- Modify: `frontend/src/lib/types.ts:138-144,255-261`
- Modify: `frontend/src/lib/api.ts:150-158,224-236`
- Modify: `frontend/src/components/session-list.tsx`
- Modify: `frontend/src/components/porto-workbench.tsx:299-302,783-841`

- [ ] **Step 1: Update types.ts**

```typescript
// Line 138: rename MemoryRecord → MessageRecord, add intent?
export type MessageRecord = {
  id: string;
  session_id: string;
  role: string;
  content: string;
  intent?: string;
  created_at: string;
};

// Backward-compat alias
export type MemoryRecord = MessageRecord;

// Line 255: add title to SessionItem
export type SessionItem = {
  session_id: string;
  title: string | null;
  first_at: string;
  last_at: string;
  message_count: number;
  preview: string;
};
```

- [ ] **Step 2: Update api.ts**

```typescript
// Rename listMemory → listMessages, change path
export async function listMessages(sessionId: string) {
  return parseJson<{ session_id: string; items: MessageRecord[] }>(
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`),
  );
}

// Backward-compat alias
export const listMemory = listMessages;

// searchMemory can be removed or kept as dead code
// (it's not imported by porto-workbench.tsx per the codebase audit)
```

- [ ] **Step 3: Update session-list.tsx — display title**

In the session rendering, change to show title:

```tsx
// In the session button content, replace session_id display with title
<span className="truncate font-medium">
  {s.title ?? s.session_id}
</span>
```

- [ ] **Step 4: Update porto-workbench.tsx — ChatLoader API call**

```typescript
// In ChatLoader (line ~783-841), replace listMemory with listMessages
// Update import: import { listMessages } from "@/lib/api"
// Change call: listMessages(sessionId) instead of listMemory(sessionId)
```

Also update `refreshMemory` (line ~299-302) to call `listMessages`.

- [ ] **Step 5: Verify frontend builds**

Run: `cd frontend && npm run build 2>&1 | tail -20`
Expected: PASS (no type errors)

> **Note:** Use `npm run build` not `tsc` for verification — per project memory, wrapper-based type checking is unreliable.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/lib/api.ts \
       frontend/src/components/session-list.tsx \
       frontend/src/components/porto-workbench.tsx
git commit -m "feat: frontend uses listMessages + displays session title"
```

---

## Task 10: 清理旧代码

**Files:**
- Delete: `backend/src/porto_chatbot/memory/store.py`
- Modify: `backend/src/porto_chatbot/memory/__init__.py` (remove MemoryStore export)
- Modify: `backend/src/porto_chatbot/models/__init__.py` (remove MemoryRecord alias)
- Modify: any remaining `MemoryStore` imports → `SessionStore` / `ConversationMemory`

- [ ] **Step 1: Search for remaining MemoryStore references**

Run: `cd backend && grep -rn "MemoryStore\|from.*store import\|get_memory" src/ --include="*.py"`
Expected: Only `store.py` itself and `__init__.py` backward-compat alias

- [ ] **Step 2: Delete store.py**

```bash
git rm backend/src/porto_chatbot/memory/store.py
```

- [ ] **Step 3: Clean up __init__.py exports**

In `memory/__init__.py`, remove:
```python
from .store import MemoryStore  # 删除
"MemoryStore",  # 从 __all__ 删除
```

In `models/__init__.py`, remove:
```python
MemoryRecord = MessageRecord  # 删除 backward-compat alias
```

- [ ] **Step 4: Run full test suite**

Run: `cd backend && uv run pytest tests/ -x -q --ignore=tests/test_langgraph_spike.py --ignore=tests/test_langgraph_orchestration_spike.py 2>&1 | tail -30`
Expected: PASS (fix any remaining import errors)

- [ ] **Step 5: Run lint**

Run: `cd backend && uv run ruff check src/ 2>&1 | tail -10`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add -A
git rm backend/src/porto_chatbot/memory/store.py  # if not already removed
git commit -m "chore: delete MemoryStore, remove backward-compat aliases"
```

- [ ] **Step 7: Full integration verification**

Run: `cd backend && uv run pytest tests/ -q 2>&1 | tail -10`

Also start the app and verify:
```bash
make backend-dev  # in another terminal or background
curl http://localhost:8100/api/sessions | python -m json.tool
curl http://localhost:8100/api/health | python -m json.tool
```
Expected: sessions list returns `{"items": [], "total": 0, "has_more": false}`; health OK

- [ ] **Step 8: Final commit if any fixes needed**

```bash
git add -A && git commit -m "fix: resolve remaining import errors after store.py deletion"
```

---

## Self-Review Checklist

### Spec coverage

| Spec 要求 | Task |
|---|---|
| sessions 表（一等实体） | T2 |
| messages 表（含 intent/indexed） | T2 |
| SessionStore 全部接口 | T2 |
| ConversationMemory 全部接口 | T3 |
| persist_turn + maybe_generate_title | T4 |
| index_and_mark（reviewer 建议 DRY） | T4 |
| compaction indexed_only | T5 |
| ChatOrchestrator handle_direct/handle_rag | T6 |
| DIRECT 路径写 SQLite 不写向量 | T6 |
| RAG 路径写 SQLite + 向量 + 回填 | T6 |
| agent_sdk on_stop persist_turn | T7 |
| search_memory ctx.session_id (C3) | T7 |
| session_metadata 移入 SessionStore (C1) | T2 |
| ChatOrchestrator KB store + dispatch (C2) | T6 |
| RAG unavailable 分支 (C4) | T6 |
| sessions.py 路由 + deps 单例 (M5) | T8 |
| 前端 title 展示 + API 路径改 | T9 |
| 删除 store.py + memory.py | T10 |
| models/__init__.py 更新 (M8) | T1+T10 |

### Placeholder scan

- `stream_chat` 的流式细节在 Task 6 中有结构说明，但完整 SSE 代码需从现有 `langchain_chat.py` 搬入（约 250 行 SSE 协议代码，原样保留，仅替换持久化调用）。这不是 placeholder——是"搬移已有代码"而非"新写代码"。

### Type consistency

- `MessageRecord` 在 T1 定义，T2/T3/T4/T5 全部引用——字段名一致（`id, session_id, role, content, intent, indexed, created_at, metadata`）
- `SessionStore.add_message` 签名在 T2 定义，T4/T6/T7 调用——参数名一致（`session_id, role, content, intent, indexed`）
- `persist_turn` 签名在 T4 定义，T6/T7 调用——关键字参数一致
- `ConversationMemory.search(session_id=必填)` 在 T3 定义，T6/T7 调用——一致
