"""SessionStore — 纯 SQLite 操作层。

管理 5 张表：sessions, messages, session_summaries, session_facts, session_metadata。
所有 chat 路径的消息持久化都经过这里；ChromaDB 向量操作在 ConversationMemory 中。
"""
from __future__ import annotations

import sqlite3
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
        self.ensure_session(session_id)
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
            intent=row[4], indexed=bool(row[5]), created_at=row[6],
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
