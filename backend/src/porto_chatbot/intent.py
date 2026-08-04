from __future__ import annotations

import re
from dataclasses import dataclass

from .llm import LLMClient
from .logging_utils import get_component_logger
from .models.enums import ChatIntent
from .settings import Settings

GREETING_RE = re.compile(
    r"^\s*(你好|您好|hi|hello|hey|哈喽|嗨|早上好|下午好|晚上好)[!！。.\s]*$",
    re.IGNORECASE,
)
DIRECT_RE = re.compile(
    r"^\s*(谢谢|感谢|thanks|thank you|你是谁|介绍一下你自己|help|帮助)[!！。.\s]*$",
    re.IGNORECASE,
)
RAG_HINTS = (
    "知识库", "文档", "资料", "根据", "查", "搜索", "分析", "拆", "设计",
    "架构", "需求", "prd", "workflow", "子系统", "支付", "风控", "订单",
)


@dataclass(frozen=True)
class IntentDecision:
    intent: ChatIntent
    reason: str


def route_chat_intent(
    message: str,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
) -> IntentDecision:
    """意图路由：LLM 优先（更准），规则降级（无 LLM 时）。

    LLM 把意图判断交给模型（function-calling 风格的结构化输出）；
    LLM 不可用或输出无效时回退到关键词/正则规则。
    """
    logger = get_component_logger("intent", settings)
    if llm is not None and llm.enabled:
        decision = _llm_route(message, llm)
        if decision is not None:
            logger.info(
                "chat intent routed llm intent=%s reason=%s message_chars=%s",
                decision.intent, decision.reason, len(message),
            )
            return decision
    decision = _rule_route(message)
    logger.info(
        "chat intent routed rule intent=%s reason=%s message_chars=%s",
        decision.intent, decision.reason, len(message),
    )
    return decision


def _llm_route(message: str, llm: LLMClient) -> IntentDecision | None:
    if not message.strip():
        return None
    parsed = llm.complete_structured(
        "你是意图分类器。判断用户消息属于：\n"
        "- direct：寒暄、闲聊、自我介绍、帮助询问，或明显不需要查询知识库的短消息\n"
        "- rag：需要查询知识库、PRD 分析、子系统设计、架构/需求/支付/风控等领问题\n"
        "只输出 JSON。",
        f"用户消息: {message[:500]}",
        {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": [e.value for e in ChatIntent]},
                "reason": {"type": "string"},
            },
            "required": ["intent", "reason"],
        },
    )
    if not isinstance(parsed, dict):
        return None
    intent = parsed.get("intent")
    if intent not in [e.value for e in ChatIntent]:
        return None
    return IntentDecision(intent, f"llm:{str(parsed.get('reason', ''))[:80]}")


def _rule_route(message: str) -> IntentDecision:
    normalized = re.sub(r"\s+", " ", message).strip()
    lower = normalized.lower()
    if not normalized:
        return IntentDecision(ChatIntent.DIRECT, "empty_message")
    if GREETING_RE.match(normalized):
        return IntentDecision(ChatIntent.DIRECT, "greeting")
    if DIRECT_RE.match(normalized):
        return IntentDecision(ChatIntent.DIRECT, "smalltalk_or_help")
    if len(normalized) <= 12 and not any(hint in lower for hint in RAG_HINTS):
        return IntentDecision(ChatIntent.DIRECT, "short_without_domain_signal")
    return IntentDecision(ChatIntent.RAG, "domain_or_knowledge_request")
