from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .llm import LLMClient
from .logging_utils import get_component_logger
from .models.enums import ChatIntent, IntentEngine, IntentRoutingMode
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

# TypeSafe System One 分类指令（与 _llm_route 的分类标准同源，随模式由 criteria 裁剪）
_TYPESAFE_BINARY_INSTRUCTIONS = (
    "判断用户消息属于 direct 还是 rag。"
    "direct：寒暄、闲聊、自我介绍、帮助询问，或明显不需要查询知识库的短消息。"
    "rag：需要查询知识库、PRD 分析、子系统设计、架构/需求/支付/风控等领域问题。"
)
_TYPESAFE_ADAPTIVE_INSTRUCTIONS = (
    "判断用户消息属于 direct、quick_rag 还是 deep_rag。"
    "direct：寒暄闲聊、无需查库。quick_rag：简单事实查询。"
    "deep_rag：复杂分析/架构/设计，需深度检索。"
)


@dataclass(frozen=True)
class IntentDecision:
    intent: ChatIntent
    reason: str
    confidence: float | None = None
    engine: str = ""  # builtin_llm / rule / typesafe；off 时为空
    fallback_from: str | None = None  # typesafe 降级时记录被放弃的引擎


def route_chat_intent(
    message: str,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    routing_mode: IntentRoutingMode = IntentRoutingMode.BINARY,
    engine: IntentEngine | str | None = None,
) -> IntentDecision:
    """意图路由（可插拔引擎，仅 Langchain chat 路径消费）。

    - ``routing_mode``: ``off`` 不分流 / ``binary`` 两分类（默认）/ ``adaptive`` 三分类。
    - ``engine``: 缺省取 ``settings.chat_intent_engine``。
      ``builtin_llm``：LLM 优先 + 规则回退（历史默认，行为不变）；
      ``rule``：纯关键词/正则，零成本；
      ``typesafe``：TypeSafe System One 外部决策引擎，带置信度门控与降级回退
      （未配置/低置信/异常时降级到 builtin 流程，fail-open）。
    """
    logger = get_component_logger("intent", settings)
    if routing_mode == IntentRoutingMode.OFF:
        return IntentDecision(ChatIntent.RAG, "routing_off")
    if engine is None:
        engine = settings.chat_intent_engine if settings else IntentEngine.BUILTIN_LLM
    engine = IntentEngine(engine)

    if engine == IntentEngine.RULE:
        decision = _rule_route(message, routing_mode)
        decision = replace(decision, engine="rule")
        logger.info(
            "chat intent routed engine=rule intent=%s reason=%s message_chars=%s",
            decision.intent, decision.reason, len(message),
        )
        return decision
    if engine == IntentEngine.TYPESAFE:
        decision = _typesafe_route(message, settings, llm, routing_mode)
        logger.info(
            "chat intent routed engine=typesafe intent=%s reason=%s "
            "confidence=%s fallback_from=%s message_chars=%s",
            decision.intent, decision.reason, decision.confidence,
            decision.fallback_from, len(message),
        )
        return decision
    return _builtin_route(message, settings, llm, routing_mode)


def _builtin_route(
    message: str,
    settings: Settings | None,
    llm: LLMClient | None,
    routing_mode: IntentRoutingMode,
) -> IntentDecision:
    """内置 LLM 优先 + 规则回退（升级前的默认路径，行为不变）。"""
    logger = get_component_logger("intent", settings)
    if llm is not None and llm.enabled:
        decision = _llm_route(message, llm, routing_mode)
        if decision is not None:
            decision = replace(decision, engine="builtin_llm")
            logger.info(
                "chat intent routed engine=builtin_llm intent=%s reason=%s message_chars=%s",
                decision.intent, decision.reason, len(message),
            )
            return decision
    decision = _rule_route(message, routing_mode)
    decision = replace(decision, engine="rule")
    logger.info(
        "chat intent routed engine=rule intent=%s reason=%s message_chars=%s",
        decision.intent, decision.reason, len(message),
    )
    return decision


def _build_typesafe_client(settings: Settings):
    """构造 TypeSafe 客户端（lazy import：未安装 typesafe-sdk 时降级路径兜底）。"""
    from typesafe_sdk import RetryPolicy, TypeSafeClient

    kwargs: dict = {
        "api_key": settings.chat_typesafe_api_key,
        # 路由预算内不允许 429/5xx 重试放大延迟
        "retry": RetryPolicy(max_retries=0),
    }
    if settings.chat_typesafe_base_url:
        kwargs["base_url"] = settings.chat_typesafe_base_url
    return TypeSafeClient(**kwargs)


def _typesafe_route(
    message: str,
    settings: Settings | None,
    llm: LLMClient | None,
    routing_mode: IntentRoutingMode,
) -> IntentDecision:
    """TypeSafe System One 分类：置信度门控 + 低置信/异常降级（fail-open）。"""
    logger = get_component_logger("intent", settings)
    empty = not message.strip()

    def _degrade(prefix: str) -> IntentDecision:
        d = _builtin_route(message, settings, llm, routing_mode)
        return replace(d, fallback_from="typesafe", reason=f"{prefix}|{d.reason}")

    if empty or settings is None or not settings.chat_typesafe_api_key:
        return _degrade("typesafe:fallback:not_configured")
    try:
        from typesafe_sdk import Choice

        client = _build_typesafe_client(settings)
        if routing_mode == IntentRoutingMode.ADAPTIVE:
            enums = ["direct", "quick_rag", "deep_rag"]
            instructions = _TYPESAFE_ADAPTIVE_INSTRUCTIONS
        else:
            enums = ["direct", "rag"]
            instructions = _TYPESAFE_BINARY_INSTRUCTIONS
        resp = client.system_one(
            state={"message": message[:_MAX_INTENT_MESSAGE_CHARS]},
            questions={
                "intent": Choice(
                    instructions=instructions,
                    criteria={name: None for name in enums},
                ),
            },
            timeout=settings.chat_typesafe_timeout_ms / 1000,
        )
        answer = resp.choices["intent"]
        choice = str(answer.choice)
        confidence = float(getattr(answer, "confidence", 0.0) or 0.0)
        if choice not in enums:
            raise ValueError(f"unexpected choice: {choice}")
        if (confidence < settings.chat_typesafe_confidence_threshold
                and settings.chat_typesafe_low_confidence_fallback):
            logger.info(
                "chat intent typesafe low_confidence=%s threshold=%s, fallback",
                confidence, settings.chat_typesafe_confidence_threshold,
            )
            return _degrade(f"typesafe:low_confidence:{confidence:.2f}")
        return IntentDecision(
            ChatIntent(choice), f"typesafe:{choice}",
            confidence=confidence, engine="typesafe",
        )
    except Exception as exc:
        logger.warning("chat intent typesafe failed, degrade to builtin: %s", exc)
        return _degrade("typesafe:fallback:error")


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
