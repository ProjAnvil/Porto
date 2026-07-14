from __future__ import annotations

import json

from ..models import SourceChunk
from .types import _BARE_JSON_RE, _JSON_FENCE_RE


def _try_parse_json(text: str) -> dict | None:
    """容忍 LLM 把 JSON 包在 fence 或夹杂文本里。"""
    if not text:
        return None
    candidates = [text]
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        candidates.insert(0, fence.group(1))
    bare = _BARE_JSON_RE.search(text)
    if bare:
        candidates.insert(0, bare.group(1))
    for cand in candidates:
        try:
            parsed = json.loads(cand)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def format_sources(sources: list[SourceChunk]) -> str:
    if not sources:
        return "无可用知识库片段。"
    return "\n\n".join(
        f"[{i + 1}] {s.path} score={s.score}\n{s.text}" for i, s in enumerate(sources)
    )
