# backend/src/porto_chatbot/agent/factory.py
"""Backend 工厂——全系统唯一一处条件判断。"""
from __future__ import annotations

from ..llm import LLMClient
from ..settings import Settings
from .backends import AgentBackend, LangchainBackend


def create_backend(
    settings: Settings,
    *,
    llm: LLMClient | None = None,
    scope: str = "workflow",
) -> AgentBackend:
    """根据 settings 的 backend 字段创建对应引擎。

    scope='chatbot' → 读 chatbot_backend；scope='workflow' → 读 workflow_backend。
    """
    backend_name = (
        settings.chatbot_backend if scope == "chatbot" else settings.workflow_backend
    )
    if backend_name == "agent_sdk":
        from ..agent_sdk.backend import AgentSDKBackend

        return AgentSDKBackend(settings)
    return LangchainBackend(llm or LLMClient(settings))
