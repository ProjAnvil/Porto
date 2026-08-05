"""SpecContext：规格生成流程的共享上下文。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..llm import LLMClient
from ..settings import Settings
from ..vector_store import LocalVectorStore


@dataclass
class SpecContext:
    """规格生成流程的共享上下文。

    ``backend`` 是节点调用的主接口（Task 2 Protocol）；
    ``llm`` 保留给 critique_spec（仍直接用 LLMClient.complete_structured）。
    若调用方仅传 ``llm`` 未传 ``backend``（向后兼容），__post_init__
    自动包一层 LangchainBackend——行为等价、现有测试不需要改。
    """

    backend: Any = None  # AgentBackend (Task 2 Protocol)
    llm: LLMClient | None = None
    state: dict[str, Any] = field(default_factory=dict)
    settings: Settings | None = None
    vector_store: LocalVectorStore | None = None
    critic_llm: LLMClient | None = None  # spec loop 评判模型，缺省用 llm
    file_service: Any = None  # FileService（final review I-1）：注入后 generate_initial_spec 的 read_file 工具可读 PRD

    def __post_init__(self) -> None:
        # 向后兼容：只传了 llm 就自动包一层 LangchainBackend
        if self.backend is None and self.llm is not None:
            from ..agent.backends import LangchainBackend

            self.backend = LangchainBackend(self.llm)
