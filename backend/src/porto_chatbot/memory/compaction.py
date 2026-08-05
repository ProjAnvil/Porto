"""会话记忆压缩（compaction）。

长会话超过阈值时，把旧消息摘要成一段 summary 并缓存，只把近期消息保留为原文。
这是 Anthropic context engineering 中的 compaction 策略：避免把整段历史塞进 context。
"""
from __future__ import annotations

from ..llm import LLMClient
from ..models import MemoryRecord
from .store import MemoryStore


def summarize_records(records: list[MemoryRecord], llm: LLMClient) -> str:
    """用 LLM 把一段对话记录压缩成中文摘要。LLM 不可用则返回空串。"""
    if not llm or not llm.enabled or not records:
        return ""
    transcript = "\n".join(f"{r.role}: {r.content}" for r in records)
    summary = llm.complete(
        "你是会话摘要助手。把以下对话历史压缩成简洁的中文摘要。\n\n"
        "强制要求:\n"
        "- 保留所有专有名词、变量名、API 名、产品名原样(不要抽象化成\"某功能\")\n"
        "- 保留所有数字、版本号、阈值\n"
        "- 保留已确认的决策(明确写\"用户确认 X\")和已否决的选项(\"用户否决 Y\")\n"
        "- 保留未解决的问题,标注\"待澄清:Z\"\n"
        "- 去掉寒暄、试探性提问、重复内容",
        f"对话历史:\n{transcript}",
    )
    return (summary or "").strip()


def get_compacted_history(
    session_id: str,
    store: MemoryStore,
    llm: LLMClient | None,
    *,
    keep_recent: int | None = None,
    threshold: int | None = None,
) -> tuple[str, list[MemoryRecord]]:
    """返回 (历史摘要, 近期原文消息)。

    - 消息数 ≤ 阈值：返回 ("", 全部消息)，不压缩。
    - 消息数 > 阈值：旧消息摘要压缩（带缓存，按 last_message_id 复用），近期 keep_recent 条保留原文。
    - LLM 不可用：不压缩，降级返回 ("", 近期 keep_recent 条)。
    """
    settings = store.settings
    keep = keep_recent if keep_recent is not None else settings.memory_recent_keep
    thresh = threshold if threshold is not None else settings.memory_compact_threshold

    records = store.get_messages_ordered(session_id)
    if len(records) <= thresh:
        return "", records

    old = records[:-keep] if keep > 0 else records
    recent = records[-keep:] if keep > 0 else []

    if not llm or not llm.enabled:
        store.logger.info("memory compaction skipped (llm disabled) session_id=%s", session_id)
        return "", recent

    last_old_id = old[-1].id if old else None
    cached = store.get_summary(session_id)
    if cached and cached.last_message_id == last_old_id and cached.summary:
        store.logger.info(
            "memory compaction cache hit session_id=%s last_message_id=%s",
            session_id, last_old_id,
        )
        return cached.summary, recent

    summary = summarize_records(old, llm)
    if summary and last_old_id:
        store.save_summary(session_id, summary, last_old_id)
    store.logger.info(
        "memory compaction done session_id=%s old=%s recent=%s summary_chars=%s",
        session_id, len(old), len(recent), len(summary),
    )
    return summary, recent
