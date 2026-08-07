"""共享编排函数——消除 DIRECT/agent_sdk/RAG 路径的消息持久化重复。

persist_turn：写 user + assistant 两条消息，可选索引向量。
index_and_mark：批量索引 + 回填 indexed flag（persist_turn 和 RAG 路径共用）。
maybe_generate_title：异步 fire-and-forget 生成 session 标题。
"""
from __future__ import annotations

import threading

from ..logging_utils import get_component_logger
from ..models import MessageRecord
from .conversation_memory import ConversationMemory
from .session_store import SessionStore

logger = get_component_logger("persist")


def index_and_mark(
    sessions: SessionStore, memory: ConversationMemory, records: list[MessageRecord],
) -> None:
    """批量 embedding + 写 ChromaDB + 回填 indexed flag。

    index 失败时只记日志——消息已在 SQLite（历史可见），只是不会被向量检索（优雅降级）。
    ChromaDB batch add 是原子的：如果它抛异常，两条消息都不会被写入向量库，
    所以跳过 mark_indexed 不会让 flag 与 ChromaDB 状态不同步。
    """
    if not records:
        return
    try:
        memory.index(records)
        sessions.mark_indexed([r.id for r in records])
        # 回填内存对象，让调用方拿到的 MessageRecord.indexed 与 DB/ChromaDB 状态一致。
        for r in records:
            r.indexed = True
    except Exception:
        logger.exception(
            "vector index failed records=%s", [r.id for r in records],
        )


def persist_turn(
    *,
    sessions: SessionStore,
    memory: ConversationMemory,
    session_id: str,
    user_content: str,
    assistant_content: str,
    intent: str,
    index_vector: bool,
) -> tuple[MessageRecord, MessageRecord]:
    """写 user + assistant 两条消息。index_vector=True 时额外写向量库 + 回填 flag。

    用于 DIRECT 路径和 agent_sdk 路径（这两条路径的 user+assistant 可以一次性写入）。
    RAG 路径不用此函数——user 消息在 LLM 之前写，assistant 在之后写，时序不同。
    """
    user_msg = sessions.add_message(
        session_id=session_id, role="user",
        content=user_content, intent=intent, indexed=False,
    )
    asst_msg = sessions.add_message(
        session_id=session_id, role="assistant",
        content=assistant_content, intent=intent, indexed=False,
    )
    if index_vector:
        index_and_mark(sessions, memory, [user_msg, asst_msg])
    return user_msg, asst_msg


def _generate_title_thread(
    sessions: SessionStore, llm, session_id: str, first_message: str,
) -> None:
    """在 daemon thread 中调 LLM 生成标题并写入 sessions 表。"""
    try:
        title = llm.complete(
            "你是会话标题生成器。用 10-15 个中文字概括以下用户消息的主题，只输出标题，不要标点。",
            f"用户消息:\n{first_message}",
        )
        title = (title or "").strip()[:50]
        if title:
            sessions.update_title(session_id, title)
            logger.info("title generated session=%s title=%s", session_id, title)
    except Exception:
        logger.exception("title generation failed session=%s", session_id)


def maybe_generate_title(
    sessions: SessionStore, llm, session_id: str, first_message: str,
) -> None:
    """session.title is None 时，fire-and-forget（daemon thread）调 LLM 生成标题。

    使用 threading.Thread(daemon=True)——在 sync 和 async 上下文中都安全。
    TOCTOU 竞争（两个并发请求都生成标题）可接受：update_title 是幂等的。
    """
    session = sessions.get_session(session_id)
    if session and session.title is not None:
        return
    if not llm or not getattr(llm, "enabled", False):
        return
    t = threading.Thread(
        target=_generate_title_thread,
        args=(sessions, llm, session_id, first_message),
        daemon=True,
    )
    t.start()
