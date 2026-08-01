"""工具执行上下文与文本格式化辅助。

包括 AgentToolContext dataclass、字符上限常量以及 _truncate / _format_chunks 辅助函数。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import SourceChunk
from ..vector_store import LocalVectorStore

State = dict[str, Any]

# 单个 tool 结果的字符上限，避免把 context 撑爆
_MAX_PRD_CHARS = 6000
_MAX_SOURCE_CHARS = 800
_MAX_TOOL_RESULT_CHARS = 6000


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，共 {len(text)} 字）"


def _format_chunks(chunks: list[SourceChunk], *, per_chunk: int = _MAX_SOURCE_CHARS) -> str:
    if not chunks:
        return "无匹配片段。"
    lines = []
    for i, c in enumerate(chunks, 1):
        lines.append(f"[{i}] {c.path} score={c.score}\n{_truncate(c.text, per_chunk)}")
    return "\n\n".join(lines)


@dataclass
class AgentToolContext:
    """工具执行上下文：持有可变的 workflow state 与向量库句柄。

    chatbot 模式额外传 memory_store / facts_store（workflow 模式为 None）。
    """

    state: State
    vector_store: LocalVectorStore | None = None
    memory_store: Any = None  # chatbot 专属（MemoryStore），workflow 为 None
    facts_store: Any = None  # chatbot 专属（SessionFactsStore），workflow 为 None
