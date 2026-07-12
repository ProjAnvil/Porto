from __future__ import annotations

from fastapi import APIRouter

from ...logging_utils import get_component_logger
from ...models import AppSettingsPayload, AppSettingsResponse
from ..deps import (
    current_settings,
    effective_agent_settings,
    effective_rag_settings,
    get_config_store,
)

logger = get_component_logger("api")

router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    settings = current_settings()
    rag_settings = effective_rag_settings()
    agent_settings = effective_agent_settings()
    return {
        "ok": True,
        "kb_path": str(settings.kb_path),
        "data_dir": str(settings.data_dir),
        "rag": {
            "vector_backend": settings.vector_backend,
            "embedding_provider": rag_settings.embedding_provider,
            "embedding_model": rag_settings.embedding_model,
            "embedding_base_url": rag_settings.embedding_base_url,
            "chunk_size": rag_settings.chunk_size,
            "chunk_overlap": rag_settings.chunk_overlap,
            "top_k": rag_settings.top_k,
        },
        "agent": {
            "agent_provider": agent_settings.agent_provider,
            "agent_model": agent_settings.agent_model,
            "agent_base_url": agent_settings.agent_base_url,
            "agent_temperature": agent_settings.agent_temperature,
            "agent_max_tokens": agent_settings.agent_max_tokens,
        },
    }


@router.get("/api/settings", response_model=AppSettingsResponse)
def get_app_settings():
    logger.info("settings read")
    return AppSettingsResponse(
        rag=effective_rag_settings(),
        agent=effective_agent_settings(),
    )


@router.put("/api/settings", response_model=AppSettingsResponse)
def save_app_settings(req: AppSettingsPayload):
    store = get_config_store()
    if req.rag:
        logger.info("settings save namespace=rag")
        store.save_rag_settings(req.rag)
    if req.agent:
        logger.info(
            "settings save namespace=agent provider=%s model=%s",
            req.agent.agent_provider,
            req.agent.agent_model,
        )
        store.save_agent_settings(req.agent)
    return get_app_settings()
