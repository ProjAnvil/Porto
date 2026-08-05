from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..models.enums import TruncationReason

Message = dict[str, Any]


class FinishReason(StrEnum):
    """归一化后的 finish_reason 已知值常量集。

    注意：``_finish_reason()`` 返回类型保持 ``str | None``（可能透传未知外部值，
    如 ``content_filter`` / ``end_turn`` / ``stop_sequence``）。此枚举仅用于
    比较点（``if x == FinishReason.LENGTH``），不用于返回值类型标注。
    """

    LENGTH = "length"  # OpenAI 语义（含 Anthropic max_tokens 归一化）
    STOP = "stop"
    TOOL_CALLS = "tool_calls"


class ContentType(StrEnum):
    """Anthropic/OpenAI content block 的 type 字段。"""

    TEXT = "text"


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
    #   TruncationReason.TOOL_LOOP_TRUNCATED  —— tool-turn 用尽仍有 tool_calls(plan 原治理)
    #   TruncationReason.MAX_TOKENS_TRUNCATED —— 单次回复被 agent_max_tokens 硬切且升级+续写均未收敛
    reason: TruncationReason | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    enabled: bool
    image_input: bool
    native_pdf: bool
    reason: str


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)
