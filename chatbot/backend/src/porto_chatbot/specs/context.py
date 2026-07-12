"""SpecContext：规格生成流程的共享上下文。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..llm import LLMClient
from ..settings import Settings
from ..vector_store import LocalVectorStore


@dataclass
class SpecContext:
    llm: LLMClient
    state: dict[str, Any]
    settings: Settings
    vector_store: LocalVectorStore | None = None
