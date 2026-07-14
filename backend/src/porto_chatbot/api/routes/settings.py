from __future__ import annotations

from fastapi import APIRouter

from ...logging_utils import get_component_logger
from ...models import AppSettingsPayload, AppSettingsResponse
from ..deps import (
    effective_agent_settings,
    effective_rag_settings,
    get_config_store,
)

logger = get_component_logger("api")

router = APIRouter()


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
