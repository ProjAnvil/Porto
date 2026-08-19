"""拒答判定（negative rejection）——词面启发式，零 LLM。

被测系统的 RAG prompt 已约束「上下文不足时明确说明」，拒答回答因此通常含
固定的致歉/声明词。误报风险（正常回答恰好含「无法」等词）由两层缓解：
marker 命中 + 回答不过长（真拒答不会长篇展开）。
"""
from __future__ import annotations

import re

REFUSAL_MARKERS = [
    # 裸「无法」/「不包含」等单 词面过宽（正常回答常见「无法转专业」），
    # 只收束到明确的拒答短语
    "无法回答", "无法确定", "无法从上下文", "无法根据上下文", "无法提供",
    "不能回答", "没有足够", "不足以回答", "不足以回应",
    "未提供", "未提及", "没有提到", "没有相关信息", "无相关信息", "无相关",
    "找不到", "没有找到", "知识库中没", "上下文中没有", "上下文未", "不在上下文",
    "抱歉",
]

# 真拒答一般不会长篇大论（含解释也不超过几百字）
_MAX_REFUSAL_CHARS = 300

# 拒答门禁阈值（批量正确率下限；措辞多样，容忍部分漏判）
REFUSAL_ACCURACY_THRESHOLD = 0.6

_COMPILED = [re.compile(re.escape(m)) for m in REFUSAL_MARKERS]


def is_refusal(answer: str) -> bool:
    """判定回答是否为拒答：命中任一 marker 且长度克制。"""
    if not answer:
        return False
    text = answer.strip()
    if len(text) > _MAX_REFUSAL_CHARS:
        return False
    return any(p.search(text) for p in _COMPILED)


def refusal_verdict(answer: str, expected_behavior: str) -> float:
    """按期望行为打分（1.0 正确 / 0.0 错误）。

    - expected=refuse：拒答 → 1；硬答（编造）→ 0
    - expected=answer：正常作答 → 1；误拒答 → 0（over-refusal 维度）
    """
    refused = is_refusal(answer)
    if expected_behavior == "refuse":
        return 1.0 if refused else 0.0
    return 0.0 if refused else 1.0
