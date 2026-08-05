from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import chromadb

from ..embeddings import EmbeddingClient
from ..logging_utils import get_component_logger
from ..models import MemoryRecord, SourceChunk
from ..settings import Settings


@dataclass
class SessionSummary:
    """缓存的会话历史摘要（compaction 命中缓存时复用）。"""

    summary: str
    last_message_id: str
    created_at: str


class MemoryStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("memory", settings)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.chroma_dir.mkdir(parents=True, exist_ok=True)
        self.embeddings = EmbeddingClient(settings)
        self.client = chromadb.PersistentClient(path=str(settings.chroma_dir))
        self.collection = self.client.get_or_create_collection(settings.memory_collection)
        self._init_db()
        self.logger.info(
            "memory store ready db=%s collection=%s",
            self.settings.memory_db_path,
            self.settings.memory_collection,
        )

    def add(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        record = MemoryRecord(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role=role,
            content=content,
            created_at=datetime.now(UTC).isoformat(),
            metadata=metadata or {},
        )
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            conn.execute(
                """
                INSERT INTO memories (id, session_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record.id, record.session_id, record.role, record.content, record.created_at),
            )
        self.logger.info(
            "memory sqlite added id=%s session_id=%s role=%s chars=%s",
            record.id, record.session_id, record.role, len(record.content),
        )
        embeddings = self.embeddings.embed_documents([record.content])
        metadatas = [{
            "session_id": record.session_id,
            "role": record.role,
            "created_at": record.created_at,
            **record.metadata,
        }]
        try:
            self.collection.add(
                ids=[record.id], documents=[record.content], metadatas=metadatas, embeddings=embeddings,
            )
        except Exception as exc:
            if "dimension" not in str(exc).lower():
                raise
            self.logger.warning("memory collection dim mismatch on add, rebuilding: %s", exc)
            self._reset_collection()
            self.collection.add(
                ids=[record.id], documents=[record.content], metadatas=metadatas, embeddings=embeddings,
            )
        self.logger.info("memory vector added id=%s session_id=%s", record.id, record.session_id)
        return record

    def list_session(self, session_id: str, limit: int = 50) -> list[MemoryRecord]:
        """倒序（新→旧），供展示与默认检索使用。"""
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, role, content, created_at
                FROM memories
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        records = [
            MemoryRecord(id=row[0], session_id=row[1], role=row[2], content=row[3], created_at=row[4])
            for row in rows
        ]
        self.logger.info("memory list session_id=%s limit=%s records=%s", session_id, limit, len(records))
        return records

    def get_messages_ordered(self, session_id: str, limit: int = 500) -> list[MemoryRecord]:
        """正序（旧→新），供 compaction 切分旧/近期消息。"""
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, role, content, created_at
                FROM memories
                WHERE session_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [
            MemoryRecord(id=row[0], session_id=row[1], role=row[2], content=row[3], created_at=row[4])
            for row in rows
        ]

    def list_sessions(
        self, date: str | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict], int]:
        """聚合所有 session（按 last_at 倒序），供前端 Sessions 列表分页。

        date 过滤 last_at 所在日期(YYYY-MM-DD)。返回 (items, total)。
        items: [{session_id, first_at, last_at, message_count, preview}]
        preview = 最后一条消息 content 截断 80 字符。

        date 过滤在 HAVING（聚合后按 MAX(created_at) 即 last_at 所在日期），
        而非 WHERE（会按任意单条消息日期过滤，多日期 session 语义错误）。
        where 保留结构以备未来按 session_id/status 等直接字段过滤扩展。
        """
        where: list[str] = []
        having: list[str] = []
        params: list[object] = []
        if date:
            having.append("substr(MAX(created_at), 1, 10) = ?")
            params.append(date)
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        having_sql = " HAVING " + " AND ".join(having) if having else ""
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute(
                f"SELECT COUNT(*) FROM (SELECT session_id FROM memories{where_sql} GROUP BY session_id{having_sql})",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"""SELECT session_id,
                           MIN(created_at) AS first_at,
                           MAX(created_at) AS last_at,
                           COUNT(*) AS message_count
                    FROM memories{where_sql}
                    GROUP BY session_id{having_sql}
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

    def search(self, query: str, *, session_id: str | None = None, top_k: int = 5) -> list[SourceChunk]:
        if self.collection.count() == 0:
            self.logger.info("memory search skipped empty collection query_chars=%s", len(query))
            return []
        self.logger.info(
            "memory search start query_chars=%s session_id=%s top_k=%s",
            len(query), session_id, top_k,
        )
        where = {"session_id": session_id} if session_id else None
        query_embedding = self.embeddings.embed_query(query)
        try:
            result = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                where=where,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            if "dimension" not in str(exc).lower():
                raise
            self.logger.warning("memory collection dim mismatch on search, rebuilding: %s", exc)
            self._reset_collection()
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
        self.logger.info("memory search finish results=%s", len(rows))
        return rows

    def get_summary(self, session_id: str) -> SessionSummary | None:
        """读取缓存的会话历史摘要。返回 SessionSummary 或 None。"""
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            row = conn.execute(
                "SELECT summary, last_message_id, created_at FROM session_summaries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return SessionSummary(summary=row[0], last_message_id=row[1], created_at=row[2])

    def save_summary(self, session_id: str, summary: str, last_message_id: str) -> None:
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            conn.execute(
                """
                INSERT INTO session_summaries (session_id, summary, last_message_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    summary = excluded.summary,
                    last_message_id = excluded.last_message_id,
                    created_at = excluded.created_at
                """,
                (session_id, summary, last_message_id, datetime.now(UTC).isoformat()),
            )
        self.logger.info(
            "memory summary saved session_id=%s last_message_id=%s chars=%s",
            session_id, last_message_id, len(summary),
        )

    def _reset_collection(self) -> None:
        """删旧 collection 并重建（embedding 维度变化等场景）。注意：会清空向量记忆。"""
        try:
            self.client.delete_collection(self.settings.memory_collection)
            self.logger.info("memory collection reset (dimension change)")
        except Exception:
            self.logger.info("memory collection reset skipped (not existed)")
        self.collection = self.client.get_or_create_collection(self.settings.memory_collection)

    def _init_db(self) -> None:
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_summaries (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    last_message_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_facts (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    source_msg_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_session ON session_facts(session_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_session_cat "
                "ON session_facts(session_id, category)"
            )
