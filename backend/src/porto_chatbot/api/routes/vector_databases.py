"""Vector database profile management API routes.

Endpoints follow spec §11 (excluding migration endpoints which arrive in Plan 3).
Credentials are write-only: never exposed in JSON, logs, or validation errors.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...logging_utils import get_component_logger
from ...models.vector_database import (
    SaveVectorProfileRequest,
    SaveVectorProfileResponse,
    VectorProfileDraft,
    VectorProfileResponse,
    VectorStoreStateResponse,
)
from ...vector_profiles import VectorProfile, VectorProfileStore
from ...vector_stores import MilvusVectorRepository
from ..deps import current_settings

logger = get_component_logger("api.vector_databases")

router = APIRouter(prefix="/api/vector-databases", tags=["vector-databases"])


def _get_store() -> VectorProfileStore:
    store = VectorProfileStore(current_settings())
    store.bootstrap()
    return store


def _to_response(profile: VectorProfile) -> VectorProfileResponse:
    return VectorProfileResponse(**profile.to_response_dict())


# ── list ──


@router.get("", response_model=VectorStoreStateResponse)
def list_profiles():
    """Return the active and pending vector database profiles."""
    store = _get_store()
    active = store.active()
    pending = store.pending()
    return VectorStoreStateResponse(
        active=_to_response(active) if active else None,
        pending=_to_response(pending) if pending else None,
        revision=store.revision(),
    )


# ── test connection ──


@router.post("/test")
def test_connection(draft: VectorProfileDraft):
    """Test connectivity to a vector database without saving.

    Returns a coarse status code — never credentials or provider exception text.
    """
    if draft.backend == "chroma":
        # Chroma is always local; no remote test needed.
        return {"status": "ok", "detail": "local backend"}
    if draft.backend == "milvus":
        try:
            repo = MilvusVectorRepository(
                uri=draft.uri or "",
                token=draft.token,
                username=draft.username,
                password=draft.password,
                database=draft.database,
                tls=draft.tls,
                timeout=min(draft.timeout, 10),
            )
            health = repo.health()
            repo.close()
            if health.healthy:
                return {"status": "ok", "detail": health.detail}
            return {"status": "error", "detail": "connection failed"}
        except Exception:
            logger.warning("milvus test connection failed uri=%s", draft.uri)
            return {"status": "error", "detail": "connection failed"}
    return {"status": "error", "detail": "unknown backend"}


# ── save (create or update pending) ──


@router.post("", response_model=SaveVectorProfileResponse)
def save_profile(req: SaveVectorProfileRequest):
    """Save a profile as pending. Returns whether migration is required."""
    import uuid

    store = _get_store()
    active = store.active()

    profile = VectorProfile(
        id=str(uuid.uuid4()),
        name=req.name,
        backend=req.backend.value if hasattr(req.backend, "value") else str(req.backend),
        metric=req.metric.value if hasattr(req.metric, "value") else str(req.metric),
        uri=req.uri,
        database=req.database,
        auth_mode=req.auth_mode,
        token=req.token,
        username=req.username,
        password=req.password,
        tls=req.tls,
        timeout=req.timeout,
        kb_collection_base=req.kb_collection_base,
        memory_collection_base=req.memory_collection_base,
    )
    store.save_pending(profile)

    # Determine if migration is required
    migration_required = active is not None and active.storage_identity() != profile.storage_identity()

    saved = store.pending()
    return SaveVectorProfileResponse(
        profile=_to_response(saved),
        migration_required=migration_required,
    )


# ── delete (inactive only) ──


@router.delete("/{profile_id}")
def delete_profile(profile_id: str):
    """Delete an inactive (pending) profile. Active profiles cannot be deleted."""
    store = _get_store()
    try:
        store.delete_inactive(profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "deleted"}
