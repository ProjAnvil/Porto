# backend/src/porto_chatbot/agent_sdk/__init__.py
"""Claude Agent SDK backend module.

Exposes AgentSDKBackend (implements agent.backends.AgentBackend Protocol) and
build_sdk_tools (wraps handlers.py functions as @tool-decorated MCP tools).
"""
from .backend import AgentSDKBackend
from .tools import build_sdk_tools

__all__ = ["AgentSDKBackend", "build_sdk_tools"]
