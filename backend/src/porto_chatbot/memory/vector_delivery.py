"""Profile-aware vector delivery tracking.

Replaces the simple ``messages.indexed`` boolean with intent/receipt pairs
that know *which profile* received each record.  This enables catch-up
after migration and exact replay.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from ..logging_utils import get_component_logger
from ..settings import Settings

logger = get_component_logger("vector_delivery")


@dataclass(frozen=True)
class VectorIndexIntent:
    """An explicit record that a message *should* be indexed."""

    seq: int
    record_id: str
    created_at: str


class VectorDeliveryStore:
    """SQLite tables in ``memory.sqlite3`` for intent/receipt tracking."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._db_path = settings.memory_db_path

    def _conn(self):
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_tables(self, conn: sqlite3.Connection) -> None:
        """Create tables if they don't exist.  Called by SessionStore._init_db."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS vector_index_intents (
                seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id   TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_intents_record ON vector_index_intents(record_id);
            CREATE TABLE IF NOT EXISTS vector_delivery_receipts (
                record_id   TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                profile_id  TEXT NOT NULL,
                delivered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                PRIMARY KEY (record_id, profile_id)
            );
        """)

    # ── intents ──

    def create_intent(self, conn: sqlite3.Connection, record_id: str) -> None:
        """Record that *record_id* should be indexed.  Call within the message
        insert transaction so intent and message are atomic."""
        conn.execute(
            "INSERT INTO vector_index_intents (record_id) VALUES (?)",
            (record_id,),
        )

    def intents_exist(self, record_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM vector_index_intents WHERE record_id=? LIMIT 1",
                (record_id,),
            ).fetchone()
        return row is not None

    def max_seq(self) -> int:
        """Highest intent sequence number (0 if no intents)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM vector_index_intents",
            ).fetchone()
        return row[0] if row else 0

    # ── receipts ──

    def mark_delivered(self, record_ids: Sequence[str], profile_id: str) -> None:
        """Mark records as delivered to *profile_id*."""
        if not record_ids:
            return
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO vector_delivery_receipts (record_id, profile_id) "
                "VALUES (?, ?)",
                [(rid, profile_id) for rid in record_ids],
            )
            conn.commit()
        logger.info(
            "receipts marked profile=%s count=%s", profile_id, len(record_ids),
        )

    def has_receipt(self, record_id: str, profile_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM vector_delivery_receipts "
                "WHERE record_id=? AND profile_id=? LIMIT 1",
                (record_id, profile_id),
            ).fetchone()
        return row is not None

    # ── replay queries ──

    def source_records_through(self, max_seq: int, profile_id: str) -> list[str]:
        """Return record_ids with intents up to *max_seq* that lack a receipt
        for *profile_id* — i.e. records that need to be indexed."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT i.record_id FROM vector_index_intents i
                   WHERE i.seq <= ?
                     AND NOT EXISTS (
                       SELECT 1 FROM vector_delivery_receipts r
                       WHERE r.record_id = i.record_id AND r.profile_id = ?
                     )
                   ORDER BY i.seq""",
                (max_seq, profile_id),
            ).fetchall()
        return [row[0] for row in rows]

    def pending_after(self, seq: int, profile_id: str) -> list[str]:
        """Return record_ids with intents after *seq* that lack a receipt
        for *profile_id*."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT i.record_id FROM vector_index_intents i
                   WHERE i.seq > ?
                     AND NOT EXISTS (
                       SELECT 1 FROM vector_delivery_receipts r
                       WHERE r.record_id = i.record_id AND r.profile_id = ?
                     )
                   ORDER BY i.seq""",
                (seq, profile_id),
            ).fetchall()
        return [row[0] for row in rows]

    def bootstrap_receipts(self, profile_id: str, record_ids: Sequence[str]) -> int:
        """Create receipts for existing indexed messages when migrating.

        Returns the number of receipts created.
        """
        if not record_ids:
            return 0
        created = 0
        with self._conn() as conn:
            for rid in record_ids:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO vector_delivery_receipts (record_id, profile_id) "
                    "VALUES (?, ?)",
                    (rid, profile_id),
                )
                created += cur.rowcount
            conn.commit()
        logger.info("bootstrap receipts profile=%s count=%s", profile_id, created)
        return created
