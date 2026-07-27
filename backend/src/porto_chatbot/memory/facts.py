from __future__ import annotations

import asyncio
import sqlite3
import threading
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


# ---------------------------------------------------------------------- #
# LLM 提取(Task 6)
# ---------------------------------------------------------------------- #

_FACTS_SCHEMA_HINT = {
    "facts": [
        {
            "category": "user_decision | user_preference | project_context | open_question",
            "content": "简洁陈述,保留专有名词/变量名/数字原样",
            "action": "add | amend | retract",
        }
    ]
}

_FACTS_SYSTEM_PROMPT = """从以下最新对话中提取值得长期记住的关键事实。

只提取用户明确表达的:
- user_decision: 用户确认或否决的决定
- user_preference: 用户表达的偏好
- project_context: 项目背景、约束、领域信息
- open_question: 待澄清的问题

不提取:agent 提问、寒暄、临时试探。无事实则返回 {"facts": []}。

强制要求:content 保留所有专有名词、变量名、数字原样;每条事实原子化,不合并。

action 语义:
- add / amend: 实现上等价,都走 upsert(模糊匹配 ≥ 阈值则更新,否则新增)
- retract: 撤销事实(用户改主意,如"不用 OAuth 了"),会撤掉匹配的同 category fact"""


def extract_facts(
    *, store: SessionFactsStore, llm, session_id: str,
    new_message: str, recent_turns: list, settings: Settings,
) -> int:
    """同步提取(供 trigger_facts_extraction 在线程/to_thread 里调用)。

    LLM 不可用 / 解析失败 / 异常 → fail-open 返回 0。
    """
    if not settings.facts_enabled or not getattr(llm, "enabled", False):
        return 0
    recent_text = "\n".join(
        f"{getattr(r, 'role', 'user')}: {getattr(r, 'content', '')}" for r in recent_turns
    )
    user_prompt = f"最新用户消息:\n{new_message}\n\n最近上下文:\n{recent_text}"
    try:
        result = llm.complete_structured(
            _FACTS_SYSTEM_PROMPT, user_prompt, _FACTS_SCHEMA_HINT,
        )
    except Exception:
        store.logger.exception("facts extract llm failed session=%s", session_id)
        return 0
    if not isinstance(result, dict):
        store.logger.info("facts extract no json session=%s", session_id)
        return 0
    facts = result.get("facts") or []
    written = 0
    for item in facts:
        category = item.get("category")
        content = (item.get("content") or "").strip()
        action = item.get("action", "add")
        if category not in _CATEGORY_PRIORITY or not content:
            continue
        if action == "retract":
            _retract_by_match(store, session_id, category, content)
            written += 1
        else:  # add / amend 等价
            store.upsert(
                session_id=session_id, category=category,
                content=content, source_msg_id=None,
            )
            written += 1
    store.logger.info(
        "facts extract done session=%s extracted=%s written=%s",
        session_id, len(facts), written,
    )
    return written


def _retract_by_match(
    store: SessionFactsStore, session_id: str, category: str, content: str,
) -> None:
    """retract 时按 Jaccard 找最匹配的 active fact 撤掉;无命中则跳过。"""
    import sqlite3

    target = set(tokens(content))
    with sqlite3.connect(store.settings.memory_db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, content FROM session_facts "
            "WHERE session_id=? AND category=? AND status='active'",
            (session_id, category),
        ).fetchall()
        best_id, best_score = None, store.settings.facts_similarity_threshold
        for row in rows:
            score = _jaccard(target, set(tokens(row["content"])))
            if score >= best_score:
                best_id, best_score = row["id"], score
        if best_id is not None:
            conn.execute(
                "UPDATE session_facts SET status='retracted' WHERE id=?", (best_id,),
            )
            store.logger.info(
                "facts retract match id=%s session=%s category=%s score=%s",
                best_id, session_id, category, round(best_score, 3),
            )


# ---------------------------------------------------------------------- #
# Triggers(Task 7): daemon 线程 / asyncio.create_task
# ---------------------------------------------------------------------- #

def trigger_facts_extraction_sync(
    *, store: SessionFactsStore, llm, session_id: str,
    new_message: str, recent_turns: list, settings: Settings,
) -> None:
    """非流式路径:开 daemon 线程 fire-and-forget。

    facts_enabled=False 时直接返回,不创建线程。
    同步路径无法在调用方等待线程结束;线程内 extract_facts 自带 fail-open。
    """
    if not settings.facts_enabled:
        return
    t = threading.Thread(
        target=extract_facts,
        kwargs=dict(
            store=store, llm=llm, session_id=session_id,
            new_message=new_message, recent_turns=list(recent_turns),
            settings=settings,
        ),
        daemon=True,
        name=f"facts-extract-{session_id}",
    )
    t.start()
    store.logger.info("facts trigger thread started session=%s", session_id)


def trigger_facts_extraction_async(
    *, store: SessionFactsStore, llm, session_id: str,
    new_message: str, recent_turns: list, settings: Settings,
):
    """流式路径:返回 asyncio.Task 供调用方 fire-and-forget(create_task)。

    内部用 asyncio.to_thread 包装同步 extract_facts,避免阻塞事件循环。
    facts 关闭时返回 None(调用方不 create_task)。
    必须在运行中的 event loop 里调用。
    """
    if not settings.facts_enabled:
        return None
    loop = asyncio.get_running_loop()
    task = loop.create_task(
        asyncio.to_thread(
            extract_facts,
            store=store, llm=llm, session_id=session_id,
            new_message=new_message, recent_turns=list(recent_turns),
            settings=settings,
        ),
        name=f"facts-extract-{session_id}",
    )
    store.logger.info("facts trigger task created session=%s", session_id)
    return task
