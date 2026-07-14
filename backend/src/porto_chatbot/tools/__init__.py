"""节点内 tool calling 的工具集。

每个节点内的 LLM 不是单次补全，而是带 tools 的 mini agent loop（见 LLMClient.complete_with_tools）。
工具通过 AgentToolContext 访问 workflow state 与向量库，把取数决策交给 LLM。
"""
from __future__ import annotations

from .context import AgentToolContext
from .registry import build_agent_tools

__all__ = ["AgentToolContext", "build_agent_tools"]
