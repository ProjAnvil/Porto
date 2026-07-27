from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

from ..embeddings import tokens
from ..logging_utils import get_component_logger
from ..models import SessionFact
from ..settings import Settings

_CATEGORY_PRIORITY = {
    "user_decision": 0,
    "user_preference": 1,
    "project_context": 2,
    "open_question": 3,
}

_CATEGORY_HEADERS: dict[str, str] = {
    "user_decision": "[决策]",
    "user_preference": "[偏好]",
    "project_context": "[背景]",
    "open_question": "[待澄清]",
}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


class SessionFactsStore:
    """session_facts 表的 CRUD 封装。upsert 用 token Jaccard 模糊匹配去重。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("facts", settings)

    def upsert(
        self, *, session_id: str, category: str, content: str,
        source_msg_id: str | None,
    ) -> str:
        now = datetime.now(UTC).isoformat()
        new_tokens = set(tokens(content))
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, content FROM session_facts "
                "WHERE session_id=? AND category=? AND status='active'",
                (session_id, category),
            ).fetchall()
            threshold = self.settings.facts_similarity_threshold
            for row in rows:
                existing_tokens = set(tokens(row["content"]))
                if _jaccard(new_tokens, existing_tokens) >= threshold:
                    conn.execute(
                        "UPDATE session_facts SET content=?, source_msg_id=?, updated_at=? "
                        "WHERE id=?",
                        (content, source_msg_id, now, row["id"]),
                    )
                    self.logger.info(
                        "facts upsert update id=%s session=%s category=%s",
                        row["id"], session_id, category,
                    )
                    return row["id"]
            fact_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO session_facts "
                "(id, session_id, category, content, status, source_msg_id, "
                " created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                (fact_id, session_id, category, content, source_msg_id, now, now),
            )
            self._enforce_cap(conn, session_id, category)
            self.logger.info(
                "facts upsert insert id=%s session=%s category=%s",
                fact_id, session_id, category,
            )
            return fact_id

    def _enforce_cap(self, conn, session_id: str, category: str) -> None:
        """超 facts_max_per_category 时,按 updated_at 淘汰最旧的 active fact。"""
        cap = self.settings.facts_max_per_category
        count = conn.execute(
            "SELECT COUNT(*) FROM session_facts "
            "WHERE session_id=? AND category=? AND status='active'",
            (session_id, category),
        ).fetchone()[0]
        if count <= cap:
            return
        to_delete = count - cap
        stale = conn.execute(
            "SELECT id FROM session_facts "
            "WHERE session_id=? AND category=? AND status='active' "
            "ORDER BY updated_at ASC LIMIT ?",
            (session_id, category, to_delete),
        ).fetchall()
        for row in stale:
            conn.execute("DELETE FROM session_facts WHERE id=?", (row["id"],))
        self.logger.info(
            "facts cap evicted session=%s category=%s count=%s", session_id, category, to_delete,
        )

    def retract(self, fact_id: str) -> None:
        """软删:标记 status='retracted'。对已 retracted 的行再 UPDATE 幂等。"""
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            conn.execute(
                "UPDATE session_facts SET status='retracted' WHERE id=?",
                (fact_id,),
            )
        self.logger.info("facts retract id=%s", fact_id)

    def list_active(self, session_id: str) -> list[SessionFact]:
        """按 category 优先级(decision>preference>context>open_question)排序。

        同 category 内按 updated_at DESC(由 by_category 保证)。
        """
        grouped = self.by_category(session_id)
        ordered: list[SessionFact] = []
        for cat in sorted(grouped, key=lambda c: _CATEGORY_PRIORITY.get(c, 99)):
            ordered.extend(grouped[cat])
        return ordered

    def by_category(self, session_id: str) -> dict[str, list[SessionFact]]:
        """分组返回 active facts,空 category 不出现在 key 中。"""
        with sqlite3.connect(self.settings.memory_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM session_facts "
                "WHERE session_id=? AND status='active' ORDER BY updated_at DESC",
                (session_id,),
            ).fetchall()
        grouped: dict[str, list[SessionFact]] = {}
        for row in rows:
            grouped.setdefault(row["category"], []).append(self._row_to_fact(row))
        return grouped

    def _row_to_fact(self, row: sqlite3.Row) -> SessionFact:
        return SessionFact(
            id=row["id"], session_id=row["session_id"], category=row["category"],
            content=row["content"], status=row["status"],
            source_msg_id=row["source_msg_id"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


def build_facts_prompt(grouped: dict[str, list[SessionFact]]) -> str:
    """按 category 优先级拼成 system prompt 片段。空输入返回 ""(调用方据此跳过插入)。"""
    if not grouped:
        return ""
    lines: list[str] = ["关键事实(用户已确认,优先参考):"]
    for cat in sorted(grouped, key=lambda c: _CATEGORY_PRIORITY.get(c, 99)):
        facts = grouped[cat]
        if not facts:
            continue
        header = _CATEGORY_HEADERS.get(cat, f"[{cat}]")
        lines.append(header)
        for f in facts:
            lines.append(f"- {f.content}")
    return "\n".join(lines)
