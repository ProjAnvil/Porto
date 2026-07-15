from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from .logging_utils import get_component_logger
from .settings import Settings

logger = get_component_logger("workflow_store")


class WorkflowStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db_path = settings.data_dir / "workflows.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id    TEXT PRIMARY KEY,
                    session_id     TEXT NOT NULL,
                    project_name   TEXT,
                    prd_text       TEXT NOT NULL,
                    top_k          INTEGER,
                    rag_snapshot   TEXT NOT NULL,
                    agent_snapshot TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    current_step   TEXT,
                    error          TEXT,
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS workflow_outputs (
                    workflow_id TEXT NOT NULL,
                    step_name   TEXT NOT NULL,
                    output      TEXT NOT NULL,
                    produced_by TEXT NOT NULL,
                    produced_at TEXT NOT NULL,
                    PRIMARY KEY (workflow_id, step_name)
                )"""
            )

    def create(self, session_id, project_name, prd_text, top_k, rag_snapshot, agent_snapshot) -> str:
        wid = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO workflows
                   (workflow_id, session_id, project_name, prd_text, top_k,
                    rag_snapshot, agent_snapshot, status, current_step, error,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (wid, session_id, project_name, prd_text, top_k,
                 json.dumps(rag_snapshot, ensure_ascii=False),
                 json.dumps(agent_snapshot, ensure_ascii=False),
                 "created", None, None, now, now),
            )
        logger.info("workflow created workflow_id=%s", wid)
        return wid

    def get(self, workflow_id) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_workflows(
        self,
        session_id=None,
        status=None,
        date=None,
        limit=50,
        offset=0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[object] = []
        if session_id:
            where.append("session_id=?")
            params.append(session_id)
        if status:
            where.append("status=?")
            params.append(status)
        if date:
            where.append("substr(created_at, 1, 10)=?")
            params.append(date)
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        with self._conn() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM workflows{where_sql}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM workflows{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [dict(r) for r in rows], total

    def save_output(self, workflow_id, step_name, output: dict, produced_by) -> None:
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO workflow_outputs (workflow_id, step_name, output, produced_by, produced_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(workflow_id, step_name)
                   DO UPDATE SET output=excluded.output, produced_by=excluded.produced_by,
                                 produced_at=excluded.produced_at""",
                (workflow_id, step_name, json.dumps(output, ensure_ascii=False), produced_by, now),
            )

    def get_outputs(self, workflow_id) -> dict[str, dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_outputs WHERE workflow_id=?", (workflow_id,)
            ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            d["output"] = json.loads(d["output"])
            out[d["step_name"]] = d
        return out

    def clear_outputs_after(self, workflow_id, step_name) -> None:
        order = ["retrieve", "understand", "identify", "generate", "evaluate"]
        keep_idx = order.index(step_name)
        victims = order[keep_idx + 1:]
        if not victims:
            return
        placeholders = ",".join("?" * len(victims))
        with self._conn() as conn:
            conn.execute(
                f"DELETE FROM workflow_outputs WHERE workflow_id=? AND step_name IN ({placeholders})",
                (workflow_id, *victims),
            )

    def update_spec(self, workflow_id, name, body) -> bool:
        """轻量更新 generate 步 output 中的 specs[name]。

        不动 produced_by/produced_at、不清下游、不改 status/current_step。
        返回 True 表示找到并更新；False 表示无 generate output、specs 非 dict
        或 name 不在 specs 中。
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT output FROM workflow_outputs"
                " WHERE workflow_id=? AND step_name='generate'",
                (workflow_id,),
            ).fetchone()
            if row is None:
                return False
            output = json.loads(row["output"])
            specs = output.get("specs")
            if not isinstance(specs, dict) or name not in specs:
                return False
            specs[name] = body
            conn.execute(
                "UPDATE workflow_outputs SET output=?"
                " WHERE workflow_id=? AND step_name='generate'",
                (json.dumps(output, ensure_ascii=False), workflow_id),
            )
        return True

    def update_status(self, workflow_id, status, current_step=None, error=None) -> None:
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT current_step FROM workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            cur = current_step if current_step is not None else (row["current_step"] if row else None)
            conn.execute(
                """UPDATE workflows SET status=?, current_step=?, error=?, updated_at=?
                   WHERE workflow_id=?""",
                (status, cur, error, now, workflow_id),
            )

    def mark_running_interrupted_on_startup(self) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                'UPDATE workflows SET status="interrupted", updated_at=? WHERE status="running"',
                (datetime.now(UTC).isoformat(),),
            )
            return cur.rowcount

    def delete(self, workflow_id) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM workflow_outputs WHERE workflow_id=?", (workflow_id,))
            conn.execute("DELETE FROM workflows WHERE workflow_id=?", (workflow_id,))
