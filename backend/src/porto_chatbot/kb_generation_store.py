"""SQLite-backed KB generation tracking for immutable rebuilds.

Each generation owns its own Chroma collection and BM25 directory.
Activation is an atomic pointer switch; failed builds never touch
the active generation.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .logging_utils import get_component_logger
from .settings import Settings

logger = get_component_logger("kb_generation_store")


@dataclass(frozen=True)
class KbGeneration:
    """One immutable build snapshot."""

    id: int
    collection_name: str
    bm25_path: str
    status: str  # building | active | inactive | failed


class KbGenerationStore:
    """SQLite table ``kb_generations`` — one row per build."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._db_path = settings.data_dir / "kb_generations.sqlite3"
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_generations (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id      TEXT NOT NULL DEFAULT 'default',
                    collection_name TEXT NOT NULL UNIQUE,
                    bm25_path       TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'building',
                    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                )
            """)
            # Migration: add profile_id to pre-existing tables
            try:
                conn.execute("SELECT profile_id FROM kb_generations LIMIT 0")
            except sqlite3.OperationalError:
                conn.execute(
                    "ALTER TABLE kb_generations ADD COLUMN profile_id TEXT NOT NULL DEFAULT 'default'",
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_gen_profile ON kb_generations(profile_id)",
            )
            conn.commit()

    # ── naming ──

    def _collection_name_for(self, gen_id: int) -> str:
        """Generation 1 keeps the legacy unsuffixed name; 2+ is suffixed."""
        if gen_id == 1:
            return self._settings.vector_collection
        return f"{self._settings.vector_collection}__g{gen_id}"

    def _bm25_path_for(self, gen_id: int) -> str:
        if gen_id == 1:
            return str(self._settings.data_dir / "bm25s_index")
        return str(self._settings.data_dir / "bm25s_index" / f"g{gen_id}")

    # ── operations ──

    def allocate(self, profile_id: str = "default") -> KbGeneration:
        """Create a new generation in ``building`` state for *profile_id*."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM kb_generations WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
            next_id = row[0] + 1
            collection_name = self._collection_name_for(next_id)
            bm25_path = self._bm25_path_for(next_id)
            conn.execute(
                "INSERT INTO kb_generations (id, profile_id, collection_name, bm25_path, status) "
                "VALUES (?, ?, ?, ?, 'building')",
                (next_id, profile_id, collection_name, bm25_path),
            )
            conn.commit()
        logger.info("generation allocated id=%s profile=%s collection=%s", next_id, profile_id, collection_name)
        return KbGeneration(
            id=next_id, collection_name=collection_name,
            bm25_path=bm25_path, status="building",
        )

    def activate(self, gen_id: int, profile_id: str = "default") -> None:
        """Atomically mark *gen_id* active and others inactive for *profile_id*."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE kb_generations SET status='inactive' WHERE status='active' AND profile_id=?",
                (profile_id,),
            )
            conn.execute(
                "UPDATE kb_generations SET status='active' WHERE id=? AND profile_id=?",
                (gen_id, profile_id),
            )
            conn.commit()
        logger.info("generation activated id=%s profile=%s", gen_id, profile_id)

    def current(self, profile_id: str = "default") -> KbGeneration | None:
        """Return the active generation for *profile_id* or ``None``."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, collection_name, bm25_path, status "
                "FROM kb_generations WHERE status='active' AND profile_id=?",
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return KbGeneration(
            id=row[0], collection_name=row[1],
            bm25_path=row[2], status=row[3],
        )

    def mark_failed(self, gen_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE kb_generations SET status='failed' WHERE id=?",
                (gen_id,),
            )
            conn.commit()
        logger.info("generation failed id=%s", gen_id)

    def bootstrap(self) -> KbGeneration | None:
        """Idempotently create generation 1 from legacy unsuffixed assets.

        Returns the bootstrapped generation if this call created it,
        or ``None`` if the table already had rows.
        """
        with self._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM kb_generations",
            ).fetchone()[0]
        if count > 0:
            return None
        collection_name = self._collection_name_for(1)
        bm25_path = self._bm25_path_for(1)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO kb_generations (id, collection_name, bm25_path, status) "
                "VALUES (1, ?, ?, 'active')",
                (collection_name, bm25_path),
            )
            conn.commit()
        logger.info("generation bootstrap id=1 collection=%s", collection_name)
        return KbGeneration(
            id=1, collection_name=collection_name,
            bm25_path=bm25_path, status="active",
        )
