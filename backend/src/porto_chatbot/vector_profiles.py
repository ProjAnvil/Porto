"""Versioned vector database profile persistence.

Profile state in ``settings.sqlite3`` is authoritative for backend selection.
One active profile and at most one pending profile are supported.  Existing
Chroma installations bootstrap as a single active Chroma profile.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

from .logging_utils import get_component_logger
from .settings import Settings
from .vector_stores import DistanceMetric

logger = get_component_logger("vector_profiles")

_PROFILE_NAME_RE = r"^[A-Za-z][A-Za-z0-9_]{2,49}$"
_COLLECTION_BASE_RE = r"^[A-Za-z][A-Za-z0-9_]{2,50}$"


class VectorProfile:
    """A persisted vector database configuration."""

    def __init__(
        self,
        *,
        id: str,
        name: str,
        backend: str,
        metric: str = DistanceMetric.L2.value,
        uri: str | None = None,
        database: str | None = None,
        auth_mode: str = "none",
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        tls: bool = False,
        timeout: int = 30,
        kb_collection_base: str = "porto_kb",
        memory_collection_base: str = "porto_memory",
        status: str = "pending",
        created_at: str | None = None,
        updated_at: str | None = None,
    ):
        self.id = id
        self.name = name
        self.backend = backend
        self.metric = metric
        self.uri = uri
        self.database = database
        self.auth_mode = auth_mode
        self._token = token
        self._username = username
        self._password = password
        self.tls = tls
        self.timeout = timeout
        self.kb_collection_base = kb_collection_base
        self.memory_collection_base = memory_collection_base
        self.status = status
        self.created_at = created_at or _now()
        self.updated_at = updated_at or self.created_at

    @property
    def credential_configured(self) -> bool:
        if self.auth_mode == "token":
            return self._token is not None
        if self.auth_mode == "user_password":
            return self._username is not None and self._password is not None
        return True  # auth_mode == "none"

    def storage_identity(self) -> tuple:
        """Tuple that determines whether a migration is required.

        Excludes timeout and credentials — rotating those does not require
        copying data.
        """
        return (
            self.backend,
            self.uri,
            self.database,
            self.tls,
            self.kb_collection_base,
            self.memory_collection_base,
            self.metric,
        )

    def to_response_dict(self) -> dict:
        """Return a credential-free dict for API responses."""
        return {
            "id": self.id,
            "name": self.name,
            "backend": self.backend,
            "metric": self.metric,
            "uri": self.uri,
            "database": self.database,
            "auth_mode": self.auth_mode,
            "tls": self.tls,
            "timeout": self.timeout,
            "kb_collection_base": self.kb_collection_base,
            "memory_collection_base": self.memory_collection_base,
            "credential_configured": self.credential_configured,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


class VectorProfileStore:
    """SQLite-backed store for vector profiles and selection state."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._db_path = settings.settings_db_path
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
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS vector_profiles (
                    id                      TEXT PRIMARY KEY,
                    name                    TEXT NOT NULL,
                    backend                 TEXT NOT NULL,
                    metric                  TEXT NOT NULL DEFAULT 'l2',
                    uri                     TEXT,
                    database_name           TEXT,
                    auth_mode               TEXT NOT NULL DEFAULT 'none',
                    token                   TEXT,
                    username                TEXT,
                    password                TEXT,
                    tls                     INTEGER NOT NULL DEFAULT 0,
                    timeout                 INTEGER NOT NULL DEFAULT 30,
                    kb_collection_base      TEXT NOT NULL,
                    memory_collection_base  TEXT NOT NULL,
                    status                  TEXT NOT NULL DEFAULT 'pending',
                    created_at              TEXT NOT NULL,
                    updated_at              TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vector_store_state (
                    id                  INTEGER PRIMARY KEY DEFAULT 1,
                    active_profile_id   TEXT,
                    pending_profile_id  TEXT,
                    revision            INTEGER NOT NULL DEFAULT 1,
                    CHECK (id = 1)
                );
            """)
            conn.commit()

    # ── row mapping ──

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> VectorProfile:
        return VectorProfile(
            id=row["id"],
            name=row["name"],
            backend=row["backend"],
            metric=row["metric"],
            uri=row["uri"],
            database=row["database_name"],
            auth_mode=row["auth_mode"],
            token=row["token"],
            username=row["username"],
            password=row["password"],
            tls=bool(row["tls"]),
            timeout=row["timeout"],
            kb_collection_base=row["kb_collection_base"],
            memory_collection_base=row["memory_collection_base"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ── bootstrap ──

    def bootstrap(self) -> VectorProfile | None:
        """Idempotently create one active Chroma profile from existing settings.

        Returns the bootstrapped profile if created, or ``None`` if the
        table already had rows.
        """
        with self._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM vector_profiles",
            ).fetchone()[0]
        if count > 0:
            return None

        now = _now()
        profile_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO vector_profiles
                   (id, name, backend, metric, uri, database_name, auth_mode,
                    token, username, password, tls, timeout,
                    kb_collection_base, memory_collection_base, status,
                    created_at, updated_at)
                   VALUES (?, ?, 'chroma', 'l2', NULL, NULL, 'none',
                           NULL, NULL, NULL, 0, 30,
                           ?, ?, 'active', ?, ?)""",
                (
                    profile_id,
                    "Default Chroma",
                    self._settings.vector_collection,
                    self._settings.memory_collection,
                    now, now,
                ),
            )
            conn.execute(
                "INSERT INTO vector_store_state (id, active_profile_id, pending_profile_id, revision) "
                "VALUES (1, ?, NULL, 1) "
                "ON CONFLICT(id) DO UPDATE SET active_profile_id=excluded.active_profile_id",
                (profile_id,),
            )
            conn.commit()
        logger.info("bootstrap profile id=%s backend=chroma", profile_id)
        return self.get(profile_id)

    # ── queries ──

    def active(self) -> VectorProfile | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT state.active_profile_id FROM vector_store_state state WHERE state.id = 1",
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return self.get(row[0])

    def pending(self) -> VectorProfile | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT state.pending_profile_id FROM vector_store_state state WHERE state.id = 1",
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return self.get(row[0])

    def get(self, profile_id: str) -> VectorProfile | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM vector_profiles WHERE id=?", (profile_id,),
            ).fetchone()
        return self._row_to_profile(row) if row else None

    def revision(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT revision FROM vector_store_state WHERE id=1",
            ).fetchone()
        return row[0] if row else 1

    # ── mutations ──

    def save_pending(self, profile: VectorProfile) -> None:
        """Insert or update a profile and mark it as pending."""
        now = _now()
        profile.updated_at = now
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO vector_profiles
                   (id, name, backend, metric, uri, database_name, auth_mode,
                    token, username, password, tls, timeout,
                    kb_collection_base, memory_collection_base, status,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, backend=excluded.backend, metric=excluded.metric,
                    uri=excluded.uri, database_name=excluded.database_name,
                    auth_mode=excluded.auth_mode,
                    token=COALESCE(excluded.token, vector_profiles.token),
                    username=COALESCE(excluded.username, vector_profiles.username),
                    password=COALESCE(excluded.password, vector_profiles.password),
                    tls=excluded.tls, timeout=excluded.timeout,
                    kb_collection_base=excluded.kb_collection_base,
                    memory_collection_base=excluded.memory_collection_base,
                    status='pending', updated_at=excluded.updated_at""",
                (
                    profile.id, profile.name, profile.backend, profile.metric,
                    profile.uri, profile.database, profile.auth_mode,
                    profile._token, profile._username, profile._password,
                    int(profile.tls), profile.timeout,
                    profile.kb_collection_base, profile.memory_collection_base,
                    profile.created_at, now,
                ),
            )
            # Set as pending in state
            current_pending = conn.execute(
                "SELECT pending_profile_id FROM vector_store_state WHERE id=1",
            ).fetchone()
            if current_pending and current_pending[0] and current_pending[0] != profile.id:
                # Delete old pending profile
                conn.execute(
                    "DELETE FROM vector_profiles WHERE id=? AND status='pending'",
                    (current_pending[0],),
                )
            conn.execute(
                "UPDATE vector_store_state SET pending_profile_id=? WHERE id=1",
                (profile.id,),
            )
            conn.commit()
        logger.info("pending profile saved id=%s name=%s", profile.id, profile.name)

    def update(self, profile_id: str, **updates) -> None:
        """Partial update of a profile. Credential fields use COALESCE."""
        allowed = {
            "name", "backend", "metric", "uri", "database", "auth_mode",
            "tls", "timeout", "kb_collection_base", "memory_collection_base",
        }
        cols = []
        vals: list = []
        for key, val in updates.items():
            if key in allowed:
                col = "database_name" if key == "database" else key
                cols.append(f"{col}=?")
                vals.append(val)
            elif key in ("token", "username", "password"):
                col = key
                cols.append(f"{col}=COALESCE(?, {col})")
                vals.append(val)
        if not cols:
            return
        cols.append("updated_at=?")
        vals.append(_now())
        vals.append(profile_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE vector_profiles SET {', '.join(cols)} WHERE id=?",
                vals,
            )
            conn.commit()

    def delete_inactive(self, profile_id: str) -> None:
        """Delete a profile only if it is not active."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM vector_profiles WHERE id=?", (profile_id,),
            ).fetchone()
            if row is None:
                return
            if row[0] == "active":
                raise ValueError("cannot delete active profile")
            conn.execute("DELETE FROM vector_profiles WHERE id=?", (profile_id,))
            conn.execute(
                "UPDATE vector_store_state SET pending_profile_id=NULL "
                "WHERE pending_profile_id=?",
                (profile_id,),
            )
            conn.commit()
        logger.info("inactive profile deleted id=%s", profile_id)

    def activate_migration(
        self,
        *,
        source_profile_id: str,
        target_profile_id: str,
    ) -> int:
        """Atomically switch the active profile to the migration target.

        One SQLite transaction:
        1. Verify the active profile is still *source_profile_id*.
        2. Verify the pending profile is still *target_profile_id*.
        3. Set target active, clear pending, increment revision.

        Returns the new revision.  Raises ``ValueError`` if the
        preconditions are not met (e.g. another activation raced ahead).
        """
        now = _now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT active_profile_id, pending_profile_id, revision "
                "FROM vector_store_state WHERE id=1",
            ).fetchone()
            if row is None:
                raise ValueError("vector_store_state row missing")
            if row["active_profile_id"] != source_profile_id:
                raise ValueError("source profile is no longer active")
            if row["pending_profile_id"] != target_profile_id:
                raise ValueError("target profile is no longer pending")

            new_revision = row["revision"] + 1
            conn.execute(
                "UPDATE vector_store_state "
                "SET active_profile_id=?, pending_profile_id=NULL, "
                "    revision=? "
                "WHERE id=1",
                (target_profile_id, new_revision),
            )
            conn.execute(
                "UPDATE vector_profiles SET status='active', updated_at=? "
                "WHERE id=?",
                (now, target_profile_id),
            )
            conn.execute(
                "UPDATE vector_profiles SET status='inactive', updated_at=? "
                "WHERE id=?",
                (now, source_profile_id),
            )
            conn.commit()
        logger.info(
            "activation committed source=%s target=%s revision=%s",
            source_profile_id, target_profile_id, new_revision,
        )
        return new_revision

    def clear_credential(self, profile_id: str, field: str) -> None:
        """Explicitly clear a credential field (set to NULL)."""
        if field not in ("token", "username", "password"):
            raise ValueError(f"cannot clear field {field!r}")
        with self._conn() as conn:
            conn.execute(
                f"UPDATE vector_profiles SET {field}=NULL, updated_at=? WHERE id=?",
                (_now(), profile_id),
            )
            conn.commit()
