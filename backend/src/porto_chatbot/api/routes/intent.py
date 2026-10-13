"""意图决策探测端点——编排页「发送测试消息」按钮的后端。

无副作用：不落库、不改配置，仅用当前生效配置（可被请求参数覆盖）跑一次
意图路由，返回决策结果与元数据（引擎/置信度/是否降级/耗时）。
"""
from __future__ import annotations

import time

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ...intent import route_chat_intent
from ...llm import LLMClient
from ...logging_utils import get_component_logger
from ...models.enums import IntentEngine, IntentRoutingMode
from ..deps import effective_intent_runtime_settings, effective_rag_chat_settings

logger = get_component_logger("api")

router = APIRouter()


class IntentTestRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    routing_mode: IntentRoutingMode | None = None
    engine: IntentEngine | None = None


class IntentTestResponse(BaseModel):
    intent: str
    reason: str
    engine: str
    confidence: float | None
    fallback: bool
    latency_ms: int


@router.post("/api/intent/test", response_model=IntentTestResponse)
def test_intent(req: IntentTestRequest) -> IntentTestResponse:
    # 注意：builtin_llm 引擎每次调用是一次真实结构化 LLM 请求。本端点与
    # /api/chat 同信任边界（本地服务），未做限流；若暴露到公网需先加速率限制。
    rag_chat = effective_rag_chat_settings()
    routing_mode = req.routing_mode or rag_chat.intent_routing_mode or IntentRoutingMode.BINARY
    engine = req.engine or rag_chat.intent_engine
    settings = effective_intent_runtime_settings()
    started = time.monotonic()
    decision = route_chat_intent(
        req.message, settings, LLMClient(settings),
        routing_mode=routing_mode, engine=engine,
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "intent test engine=%s mode=%s intent=%s confidence=%s fallback=%s latency_ms=%s",
        decision.engine, routing_mode, decision.intent,
        decision.confidence, decision.fallback_from, latency_ms,
    )
    return IntentTestResponse(
        intent=str(decision.intent),
        reason=decision.reason,
        engine=decision.engine,
        confidence=decision.confidence,
        fallback=decision.fallback_from is not None,
        latency_ms=latency_ms,
    )
