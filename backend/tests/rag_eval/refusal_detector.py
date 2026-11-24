"""Refusal detection (negative rejection): a lexical heuristic, no LLM.

The RAG prompt under test already requires the model to say so explicitly when the
context is insufficient, so refusals usually contain a fixed apology / declaration phrase.
The false-positive risk (a normal answer that happens to contain a word such as "无法") is
mitigated in two layers: a marker hit + a not-too-long answer (a real refusal does not run
on at length).
"""
from __future__ import annotations

import re

REFUSAL_MARKERS = [
    # A bare "无法" / "不包含" is too broad as a lexical cue ("无法转专业" is common in normal
    # answers), so only explicit refusal phrases are collected
    "无法回答", "无法确定", "无法从上下文", "无法根据上下文", "无法提供",
    "不能回答", "没有足够", "不足以回答", "不足以回应",
    "未提供", "未提及", "没有提到", "没有相关信息", "无相关信息", "无相关",
    "找不到", "没有找到", "知识库中没", "上下文中没有", "上下文未", "不在上下文",
    "抱歉",
]

# A real refusal is rarely long-winded (even with an explanation it stays under a few
# hundred characters)
_MAX_REFUSAL_CHARS = 300

# Refusal gate threshold (lower bound on batch accuracy; wording varies, so a few missed
# refusals are tolerated)
REFUSAL_ACCURACY_THRESHOLD = 0.6

_COMPILED = [re.compile(re.escape(m)) for m in REFUSAL_MARKERS]


def is_refusal(answer: str) -> bool:
    """Decide whether an answer is a refusal: at least one marker hit and a restrained length."""
    if not answer:
        return False
    text = answer.strip()
    if len(text) > _MAX_REFUSAL_CHARS:
        return False
    return any(p.search(text) for p in _COMPILED)


def refusal_verdict(answer: str, expected_behavior: str) -> float:
    """Score an answer against the expected behavior (1.0 correct / 0.0 wrong).

    - expected=refuse: refusal → 1; answering anyway (fabricating) → 0
    - expected=answer: normal answer → 1; wrongly refusing → 0 (the over-refusal dimension)
    """
    refused = is_refusal(answer)
    if expected_behavior == "refuse":
        return 1.0 if refused else 0.0
    return 0.0 if refused else 1.0
