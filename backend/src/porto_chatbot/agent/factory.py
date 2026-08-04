# backend/src/porto_chatbot/agent/factory.py
"""Backend 工厂——全系统唯一一处条件判断。"""
from __future__ import annotations

from enum import StrEnum

from ..llm import LLMClient
from ..models.enums import ChatbotBackend
from ..settings import Settings
from .backends import AgentBackend, LangchainBackend


class BackendScope(StrEnum):
    """create_backend 的 scope 参数：区分 chatbot / workflow 配置。"""

    CHATBOT = "chatbot"
    WORKFLOW = "workflow"


def create_backend(
    settings: Settings,
    *,
    llm: LLMClient | None = None,
    scope: BackendScope | str = BackendScope.WORKFLOW,
) -> AgentBackend:
    """根据 settings 的 backend 字段创建对应引擎。

    scope='chatbot' → 读 chatbot_backend；scope='workflow' → 读 workflow_backend。
    """
    backend_name = (
        settings.chatbot_backend if scope == BackendScope.CHATBOT else settings.workflow_backend
    )
    if backend_name == ChatbotBackend.AGENT_SDK:
        from ..agent_sdk.backend import AgentSDKBackend

        return AgentSDKBackend(settings)
    return LangchainBackend(llm or LLMClient(settings))
