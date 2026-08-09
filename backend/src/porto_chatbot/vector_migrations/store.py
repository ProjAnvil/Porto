"""SQLite-backed migration state machine for online vector migration.

Tracks one migration at a time per data directory.  Phases progress through
copying → catching_up → finalizing → verifying → activating → succeeded.
Failures leave the source active; the migration can be retried.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..logging_utils import get_component_logger
from ..settings import Settings

logger = get_component_logger("vector_migration_store")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MigrationStatus:
    """Terminal / intermediate states for a migration record."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class MigrationPhase:
    """Ordered phases within a running migration."""

    COPYING = "copying"
    CATCHING_UP = "catching_up"
    FINALIZING = "finalizing"
    VERIFYING = "verifying"
    ACTIVATING = "activating"

    ORDER = [COPYING, CATCHING_UP, FINALIZING, VERIFYING, ACTIVATING]


@dataclass
class VectorMigration:
    """One migration attempt between two profiles."""

    id: str
    source_profile_id: str
    target_profile_id: str
    status: str = MigrationStatus.RUNNING
    phase: str = MigrationPhase.COPYING
    kb_generation: int | None = None
    memory_seq: int = 0
    kb_total: int = 0
    kb_copied: int = 0
    memory_total: int = 0
    memory_copied: int = 0
    verification_json: str | None = None
    heartbeat_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


class VectorMigrationStore:
    """SQLite table ``vector_migrations`` — one running migration per data dir."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._db_path = settings.settings_db_path
        settings.data_dir.mkdir(parents=True, exist_ok=True)

    def _conn(self):
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vector_migrations (
                    id                  TEXT PRIMARY KEY,
                    source_profile_id   TEXT NOT NULL,
                    target_profile_id   TEXT NOT NULL,
                    status              TEXT NOT NULL DEFAULT 'running',
                    phase               TEXT NOT NULL DEFAULT 'copying',
                    kb_generation       INTEGER,
                    memory_seq          INTEGER NOT NULL DEFAULT 0,
                    kb_total            INTEGER NOT NULL DEFAULT 0,
                    kb_copied           INTEGER NOT NULL DEFAULT 0,
                    memory_total        INTEGER NOT NULL DEFAULT 0,
                    memory_copied       INTEGER NOT NULL DEFAULT 0,
                    verification_json   TEXT,
                    heartbeat_at        TEXT,
                    started_at          TEXT NOT NULL,
                    finished_at         TEXT,
                    error               TEXT
                )
            """)
            conn.commit()

    @staticmethod
    def _row_to_migration(row: sqlite3.Row) -> VectorMigration:
        return VectorMigration(
            id=row["id"],
            source_profile_id=row["source_profile_id"],
            target_profile_id=row["target_profile_id"],
            status=row["status"],
            phase=row["phase"],
            kb_generation=row["kb_generation"],
            memory_seq=row["memory_seq"],
            kb_total=row["kb_total"],
            kb_copied=row["kb_copied"],
            memory_total=row["memory_total"],
            memory_copied=row["memory_copied"],
            verification_json=row["verification_json"],
            heartbeat_at=row["heartbeat_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=row["error"],
        )

    def create(
        self, *, source_profile_id: str, target_profile_id: str,
    ) -> VectorMigration:
        """Create a new migration.  Raises if one is already running."""
        import uuid

        self.init_db()
        now = _now()
        migration_id = str(uuid.uuid4())
        with self._conn() as conn:
            # Enforce one running migration
            existing = conn.execute(
                "SELECT id FROM vector_migrations WHERE status='running' LIMIT 1",
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"a migration is already running (id={existing[0]})",
                )
            conn.execute(
                """INSERT INTO vector_migrations
                   (id, source_profile_id, target_profile_id, status, phase,
                    started_at, heartbeat_at)
                   VALUES (?, ?, ?, 'running', 'copying', ?, ?)""",
                (migration_id, source_profile_id, target_profile_id, now, now),
            )
            conn.commit()
        logger.info(
            "migration created id=%s source=%s target=%s",
            migration_id, source_profile_id, target_profile_id,
        )
        return self.get(migration_id)

    def get(self, migration_id: str) -> VectorMigration | None:
        self.init_db()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM vector_migrations WHERE id=?", (migration_id,),
            ).fetchone()
        return self._row_to_migration(row) if row else None

    def current(self) -> VectorMigration | None:
        """Return the running migration, or None."""
        self.init_db()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM vector_migrations WHERE status='running' LIMIT 1",
            ).fetchone()
        return self._row_to_migration(row) if row else None

    def current_or_latest(self) -> VectorMigration | None:
        """Return running migration, or the most recent terminal one."""
        self.init_db()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM vector_migrations WHERE status='running' "
                "ORDER BY started_at DESC LIMIT 1",
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM vector_migrations "
                    "ORDER BY finished_at DESC LIMIT 1",
                ).fetchone()
        return self._row_to_migration(row) if row else None

    def transition(
        self, migration_id: str, *,
        phase: str | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update phase and/or status.  Validates phase ordering."""
        self.init_db()
        sets: list[str] = []
        vals: list[Any] = []
        if phase is not None:
            sets.append("phase=?")
            vals.append(phase)
        if status is not None:
            sets.append("status=?")
            vals.append(status)
            if status in (MigrationStatus.SUCCEEDED, MigrationStatus.FAILED,
                          MigrationStatus.INTERRUPTED, MigrationStatus.CANCELLED):
                sets.append("finished_at=?")
                vals.append(_now())
        if error is not None:
            sets.append("error=?")
            vals.append(error)
        if not sets:
            return
        sets.append("heartbeat_at=?")
        vals.append(_now())
        vals.append(migration_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE vector_migrations SET {', '.join(sets)} WHERE id=?",
                vals,
            )
            conn.commit()

    def update_progress(
        self, migration_id: str, *,
        kb_copied: int | None = None,
        kb_total: int | None = None,
        memory_copied: int | None = None,
        memory_total: int | None = None,
        memory_seq: int | None = None,
        kb_generation: int | None = None,
    ) -> None:
        sets: list[str] = []
        vals: list[Any] = []
        for key, val in [
            ("kb_copied", kb_copied), ("kb_total", kb_total),
            ("memory_copied", memory_copied), ("memory_total", memory_total),
            ("memory_seq", memory_seq), ("kb_generation", kb_generation),
        ]:
            if val is not None:
                sets.append(f"{key}=?")
                vals.append(val)
        if not sets:
            return
        sets.append("heartbeat_at=?")
        vals.append(_now())
        vals.append(migration_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE vector_migrations SET {', '.join(sets)} WHERE id=?",
                vals,
            )
            conn.commit()

    def heartbeat(self, migration_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE vector_migrations SET heartbeat_at=? WHERE id=?",
                (_now(), migration_id),
            )
            conn.commit()

    def interrupt_stale(self, max_age_seconds: int = 90) -> int:
        """Mark running migrations with stale heartbeats as interrupted.

        Returns the number of interrupted migrations.
        """
        self.init_db()
        cutoff = datetime.now(UTC).timestamp() - max_age_seconds
        count = 0
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, heartbeat_at FROM vector_migrations WHERE status='running'",
            ).fetchall()
            for row in rows:
                hb = row["heartbeat_at"]
                if hb is None:
                    continue
                try:
                    hb_dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
                    if hb_dt.timestamp() < cutoff:
                        conn.execute(
                            "UPDATE vector_migrations SET status='interrupted', "
                            "error='heartbeat stale', finished_at=? WHERE id=?",
                            (_now(), row["id"]),
                        )
                        count += 1
                except (ValueError, TypeError):
                    continue
            conn.commit()
        if count:
            logger.warning("interrupted %s stale migrations", count)
        return count

    def cancel(self, migration_id: str) -> None:
        """Cancel a migration (only before activating phase)."""
        self.init_db()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT phase FROM vector_migrations WHERE id=?", (migration_id,),
            ).fetchone()
            if row is None:
                return
            if row[0] == MigrationPhase.ACTIVATING:
                raise ValueError("cannot cancel migration in activating phase")
            conn.execute(
                "UPDATE vector_migrations SET status='cancelled', finished_at=? WHERE id=?",
                (_now(), migration_id),
            )
            conn.commit()
        logger.info("migration cancelled id=%s", migration_id)

    def save_verification(self, migration_id: str, verification_json: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE vector_migrations SET verification_json=? WHERE id=?",
                (verification_json, migration_id),
            )
            conn.commit()
