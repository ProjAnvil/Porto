from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Message = dict[str, Any]


@dataclass
class ToolDef:
    """单个工具定义：schema 给 LLM，handler 在本地执行。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result: str


@dataclass
class ToolLoopResult:
    """complete_with_tools 的返回。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    turns: int = 0
    truncated: bool = False
    # 截断原因(仅 truncated=True 时有意义,供节点层透传到 tool_meta.reason):
    #   "tool_loop_truncated"   —— tool-turn 用尽仍有 tool_calls(plan 原治理)
    #   "max_tokens_truncated"  —— 单次回复被 agent_max_tokens 硬切且升级+续写均未收敛
    reason: str | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    enabled: bool
    image_input: bool
    native_pdf: bool
    reason: str


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)
