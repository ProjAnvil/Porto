from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .logging_utils import get_component_logger
from .models import IndexJobStatus, IndexStats
from .settings import Settings

logger = get_component_logger("locking")

RAG_INDEX_LOCK = "rag_index"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class DbLockStore:
    """``service_locks`` 表的访问层。

    表结构是通用的（按 ``name`` 区分不同临界区），当前仅 RAG 索引使用
    (``name='rag_index'``)。单进程内互斥由 :class:`IndexSupervisor` 的进程内锁
    保证；本表承载**可恢复的状态**（status / progress / heartbeat / owner_pid），
    重启后用于识别上次崩溃残留并就地归位（``running`` → ``interrupted``），
    避免 reindex 永远卡在 busy。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db_path = settings.settings_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()
        self.ensure_row()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS service_locks (
                    name             TEXT PRIMARY KEY,
                    status           TEXT NOT NULL DEFAULT 'idle',
                    owner_pid        INTEGER,
                    source           TEXT,
                    reset            INTEGER NOT NULL DEFAULT 1,
                    progress_done    INTEGER NOT NULL DEFAULT 0,
                    progress_total   INTEGER NOT NULL DEFAULT 0,
                    chunks_done      INTEGER NOT NULL DEFAULT 0,
                    started_at       TEXT,
                    heartbeat_at     TEXT,
                    finished_at      TEXT,
                    last_indexed_at  TEXT,
                    stats_json       TEXT,
                    error            TEXT,
                    updated_at       TEXT NOT NULL
                )
                """
            )

    def ensure_row(self, name: str = RAG_INDEX_LOCK) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO service_locks (name, status, updated_at) VALUES (?, 'idle', ?)",
                (name, _now_iso()),
            )

    # ---------------- 读取 ----------------
    def get(self, name: str = RAG_INDEX_LOCK) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM service_locks WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def get_status(self, name: str = RAG_INDEX_LOCK) -> IndexJobStatus:
        row = self.get(name)
        return self._row_to_status(row) if row else IndexJobStatus()

    @staticmethod
    def _row_to_status(row: dict[str, Any]) -> IndexJobStatus:
        last_stats: IndexStats | None = None
        stats_raw = row.get("stats_json")
        if stats_raw:
            try:
                last_stats = IndexStats.model_validate_json(stats_raw)
            except Exception:
                logger.exception("decode stats_json failed raw_chars=%s", len(stats_raw))
        return IndexJobStatus(
            status=row["status"],
            source=row.get("source"),
            reset=bool(row.get("reset", 1)),
            progress_done=row.get("progress_done", 0),
            progress_total=row.get("progress_total", 0),
            chunks_done=row.get("chunks_done", 0),
            started_at=row.get("started_at"),
            heartbeat_at=row.get("heartbeat_at"),
            finished_at=row.get("finished_at"),
            last_indexed_at=row.get("last_indexed_at"),
            last_stats=last_stats,
            error=row.get("error"),
        )

    # ---------------- 状态机转移 ----------------
    def mark_running(self, name: str, *, source: str, reset: bool, total: int) -> None:
        self.ensure_row(name)
        now = _now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE service_locks SET
                    status = 'running', owner_pid = ?, source = ?, reset = ?,
                    progress_done = 0, progress_total = ?, chunks_done = 0,
                    started_at = ?, heartbeat_at = ?, finished_at = NULL,
                    stats_json = NULL, error = NULL, updated_at = ?
                WHERE name = ?
                """,
                (os.getpid(), source, int(reset), total, now, now, now, name),
            )
        logger.info("lock mark running name=%s source=%s reset=%s total=%s", name, source, reset, total)

    def update_progress(
        self,
        name: str,
        *,
        done: int,
        total: int | None = None,
        chunks_done: int | None = None,
    ) -> None:
        now = _now_iso()
        sets: list[str] = ["progress_done = ?", "heartbeat_at = ?", "updated_at = ?"]
        params: list[Any] = [done, now, now]
        if total is not None:
            sets.append("progress_total = ?")
            params.append(total)
        if chunks_done is not None:
            sets.append("chunks_done = ?")
            params.append(chunks_done)
        params.append(name)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE service_locks SET {', '.join(sets)} WHERE name = ?",
                params,
            )

    def mark_succeeded(self, name: str, *, stats: IndexStats) -> None:
        now = _now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE service_locks SET
                    status = 'succeeded', progress_done = progress_total,
                    heartbeat_at = ?, finished_at = ?, last_indexed_at = ?,
                    stats_json = ?, error = NULL, updated_at = ?
                WHERE name = ?
                """,
                (now, now, now, stats.model_dump_json(), now, name),
            )
        logger.info("lock mark succeeded name=%s chunks=%s", name, stats.chunks)

    def mark_failed(self, name: str, *, error: str) -> None:
        now = _now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE service_locks SET
                    status = 'failed', finished_at = ?, error = ?, updated_at = ?
                WHERE name = ?
                """,
                (now, error, now, name),
            )
        logger.warning("lock mark failed name=%s error=%s", name, error)

    def mark_interrupted(self, name: str, *, error: str) -> int:
        """启动清理：把残留的 ``running`` 行就地归位为 ``interrupted``，**不触发重建**。

        返回受影响行数（1 表示确实清理了一处上次崩溃残留）。
        """
        now = _now_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE service_locks SET
                    status = 'interrupted', finished_at = ?, error = ?, updated_at = ?
                WHERE name = ? AND status = 'running'
                """,
                (now, error, now, name),
            )
        affected = cur.rowcount or 0
        if affected:
            logger.warning("lock cleared stale running name=%s", name)
        return affected
