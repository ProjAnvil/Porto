from __future__ import annotations

from ..agent import PortoAgent
from ..config_store import ConfigStore
from ..llm import LLMClient
from ..logging_utils import get_component_logger
from ..memory import MemoryStore
from ..models import AgentSettingsPayload, RagSettingsPayload
from ..vector_store import LocalVectorStore

logger = get_component_logger("api")


def current_settings():
    """Return the active settings singleton.

    Resolved lazily through the ``porto_chatbot.main`` shim so that tests which
    ``monkeypatch.setattr(main, "settings", ...)`` see their patch applied to
    every dependency factory and route handler. Importing ``main`` at call time
    (rather than at module load time) avoids a circular import.
    """
    from porto_chatbot import main as _main

    return _main.settings


def get_config_store() -> ConfigStore:
    return ConfigStore(current_settings())


def default_rag_settings() -> RagSettingsPayload:
    settings = current_settings()
    return RagSettingsPayload(
        embedding_provider=settings.embedding_provider,
        embedding_model=settings.embedding_model,
        embedding_base_url=settings.embedding_base_url,
        chunk_size=settings.max_chunk_chars,
        chunk_overlap=settings.chunk_overlap,
        top_k=settings.top_k,
    )


def default_agent_settings() -> AgentSettingsPayload:
    settings = current_settings()
    return AgentSettingsPayload(
        agent_provider=settings.agent_provider,
        agent_model=settings.agent_model,
        agent_base_url=settings.agent_base_url,
        agent_api_key=settings.agent_api_key,
        agent_temperature=settings.agent_temperature,
        agent_max_tokens=settings.agent_max_tokens,
        critic_provider=settings.critic_provider,
        critic_model=settings.critic_model,
        critic_base_url=settings.critic_base_url,
        critic_api_key=settings.critic_api_key,
        critic_temperature=settings.critic_temperature,
        critic_max_tokens=settings.critic_max_tokens,
        spec_refine_enabled=settings.spec_refine_enabled,
        spec_refine_max_iter=settings.spec_refine_max_iter,
        spec_refine_parallel=settings.spec_refine_parallel,
        spec_refine_pass_score=settings.spec_refine_pass_score,
        spec_refine_budget_tokens=settings.spec_refine_budget_tokens,
        workflow_rework_enabled=settings.workflow_rework_enabled,
        workflow_rework_max_passes=settings.workflow_rework_max_passes,
        memory_compact_threshold=settings.memory_compact_threshold,
        memory_recent_keep=settings.memory_recent_keep,
        context_char_budget=settings.context_char_budget,
        agent_stream_enabled=settings.agent_stream_enabled,
        agent_max_tool_turns=settings.agent_max_tool_turns,
    )


def effective_rag_settings(payload: RagSettingsPayload | None = None) -> RagSettingsPayload:
    updates = default_rag_settings().model_dump(exclude_none=True)
    updates.update(get_config_store().get_rag_settings().model_dump(exclude_none=True))
    if payload:
        updates.update(payload.model_dump(exclude_none=True))
    return RagSettingsPayload(**updates)


def effective_agent_settings(payload: AgentSettingsPayload | None = None) -> AgentSettingsPayload:
    updates = default_agent_settings().model_dump(exclude_none=True)
    updates.update(get_config_store().get_agent_settings().model_dump(exclude_none=True))
    if payload:
        updates.update(payload.model_dump(exclude_none=True))
    return AgentSettingsPayload(**updates)


def apply_rag_settings(
    payload: RagSettingsPayload | None = None,
    agent: AgentSettingsPayload | None = None,
    **extra,
):
    settings = current_settings()
    updates = effective_agent_settings(agent).model_dump(exclude_none=True)
    updates.update(effective_rag_settings(payload).model_dump(exclude_none=True))
    updates.update(extra)
    if "chunk_size" in updates:
        updates["max_chunk_chars"] = updates.pop("chunk_size")
    return settings.model_copy(update=updates)


def get_store(runtime_settings=None) -> LocalVectorStore:
    return LocalVectorStore(runtime_settings or current_settings())


def get_agent() -> PortoAgent:
    settings = current_settings()
    return PortoAgent(settings, get_store(), LLMClient(settings))


def get_memory(runtime_settings=None) -> MemoryStore:
    return MemoryStore(runtime_settings or current_settings())
