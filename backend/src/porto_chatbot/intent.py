from __future__ import annotations

import re
from dataclasses import dataclass

from .llm import LLMClient
from .logging_utils import get_component_logger
from .models.enums import ChatIntent, IntentRoutingMode
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
# adaptive 模式规则降级时判定 deep_rag 的复杂度关键词
_DEEP_HINTS = ("分析", "架构", "设计", "拆", "完整", "详细", "怎么实现")

_MAX_INTENT_MESSAGE_CHARS = 500
_MAX_REASON_CHARS = 80
_SHORT_MESSAGE_THRESHOLD = 12


@dataclass(frozen=True)
class IntentDecision:
    intent: ChatIntent
    reason: str


def route_chat_intent(
    message: str,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    routing_mode: IntentRoutingMode = IntentRoutingMode.BINARY,
) -> IntentDecision:
    """意图路由。

    - ``off``：不分流（调用方直接走 RAG），返回 ``RAG`` + ``routing_off``。
    - ``binary``：``direct`` / ``rag`` 两分类（默认，向后兼容）。
    - ``adaptive``：``direct`` / ``quick_rag`` / ``deep_rag`` 三分类。

    LLM 优先（更准），不可用或输出无效时回退到关键词/正则规则。
    """
    logger = get_component_logger("intent", settings)
    if routing_mode == IntentRoutingMode.OFF:
        return IntentDecision(ChatIntent.RAG, "routing_off")
    if llm is not None and llm.enabled:
        decision = _llm_route(message, llm, routing_mode)
        if decision is not None:
            logger.info(
                "chat intent routed llm intent=%s reason=%s message_chars=%s",
                decision.intent, decision.reason, len(message),
            )
            return decision
    decision = _rule_route(message, routing_mode)
    logger.info(
        "chat intent routed rule intent=%s reason=%s message_chars=%s",
        decision.intent, decision.reason, len(message),
    )
    return decision


def _llm_route(
    message: str, llm: LLMClient, routing_mode: IntentRoutingMode,
) -> IntentDecision | None:
    """LLM 结构化分类。

    按模式裁剪 schema enum：binary 仅暴露 ``[direct, rag]``，
    adaptive 暴露 ``[direct, quick_rag, deep_rag]``，避免 quick_rag/deep_rag
    在 binary 模式下渗漏到 LLM 输出（Task 1 review 观察）。
    """
    if not message.strip():
        return None
    if routing_mode == IntentRoutingMode.ADAPTIVE:
        enums = ["direct", "quick_rag", "deep_rag"]
        desc = (
            "- direct：寒暄闲聊、无需查库\n"
            "- quick_rag：简单事实查询\n"
            "- deep_rag：复杂分析/架构/设计，需深度检索"
        )
    else:
        enums = ["direct", "rag"]
        desc = (
            "- direct：寒暄、闲聊、自我介绍、帮助询问，"
            "或明显不需要查询知识库的短消息\n"
            "- rag：需要查询知识库、PRD 分析、子系统设计、"
            "架构/需求/支付/风控等领问题"
        )
    parsed = llm.complete_structured(
        f"你是意图分类器。判断用户消息属于：\n{desc}\n只输出 JSON。",
        f"用户消息: {message[:_MAX_INTENT_MESSAGE_CHARS]}",
        {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": enums},
                "reason": {"type": "string"},
            },
            "required": ["intent", "reason"],
        },
    )
    if not isinstance(parsed, dict):
        return None
    intent = parsed.get("intent")
    if intent not in enums:
        return None
    return IntentDecision(intent, f"llm:{str(parsed.get('reason', ''))[:_MAX_REASON_CHARS]}")


def _rule_route(
    message: str, routing_mode: IntentRoutingMode = IntentRoutingMode.BINARY,
) -> IntentDecision:
    """关键词/正则降级路由。

    binary/off 输出 ``RAG``；adaptive 按 ``_DEEP_HINTS`` 区分 ``DEEP_RAG``
    与 ``QUICK_RAG``。
    """
    normalized = re.sub(r"\s+", " ", message).strip()
    lower = normalized.lower()
    if not normalized:
        return IntentDecision(ChatIntent.DIRECT, "empty_message")
    if GREETING_RE.match(normalized):
        return IntentDecision(ChatIntent.DIRECT, "greeting")
    if DIRECT_RE.match(normalized):
        return IntentDecision(ChatIntent.DIRECT, "smalltalk_or_help")
    if len(normalized) <= _SHORT_MESSAGE_THRESHOLD and not any(hint in lower for hint in RAG_HINTS):
        return IntentDecision(ChatIntent.DIRECT, "short_without_domain_signal")
    if routing_mode == IntentRoutingMode.ADAPTIVE:
        if any(h in normalized for h in _DEEP_HINTS):
            return IntentDecision(ChatIntent.DEEP_RAG, "deep_domain_request")
        return IntentDecision(ChatIntent.QUICK_RAG, "domain_or_knowledge_request")
    return IntentDecision(ChatIntent.RAG, "domain_or_knowledge_request")
