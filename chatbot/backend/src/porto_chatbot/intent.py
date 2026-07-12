from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .logging_utils import get_component_logger
from .settings import Settings

ChatIntent = Literal["direct", "rag"]

GREETING_RE = re.compile(
    r"^\s*(你好|您好|hi|hello|hey|哈喽|嗨|早上好|下午好|晚上好)[!！。.\s]*$",
    re.IGNORECASE,
)
DIRECT_RE = re.compile(
    r"^\s*(谢谢|感谢|thanks|thank you|你是谁|介绍一下你自己|help|帮助)[!！。.\s]*$",
    re.IGNORECASE,
)
RAG_HINTS = (
    "知识库",
    "文档",
    "资料",
    "根据",
    "查",
    "搜索",
    "分析",
    "拆",
    "设计",
    "架构",
    "需求",
    "prd",
    "workflow",
    "子系统",
    "支付",
    "风控",
    "订单",
)


@dataclass(frozen=True)
class IntentDecision:
    intent: ChatIntent
    reason: str


def route_chat_intent(message: str, settings: Settings | None = None) -> IntentDecision:
    logger = get_component_logger("intent", settings)
    normalized = re.sub(r"\s+", " ", message).strip()
    lower = normalized.lower()

    if not normalized:
        decision = IntentDecision("direct", "empty_message")
    elif GREETING_RE.match(normalized):
        decision = IntentDecision("direct", "greeting")
    elif DIRECT_RE.match(normalized):
        decision = IntentDecision("direct", "smalltalk_or_help")
    elif len(normalized) <= 12 and not any(hint in lower for hint in RAG_HINTS):
        decision = IntentDecision("direct", "short_without_domain_signal")
    else:
        decision = IntentDecision("rag", "domain_or_knowledge_request")

    logger.info(
        "chat intent routed intent=%s reason=%s message_chars=%s",
        decision.intent,
        decision.reason,
        len(message),
    )
    return decision
