"""Vector migration API routes — lifecycle endpoints for online migration.

Endpoints:
- POST   /api/vector-databases/migrate        — start migration
- GET    /api/vector-databases/migrate        — current status
- POST   /api/vector-databases/migrate/retry  — retry after failure
- POST   /api/vector-databases/migrate/cancel — cancel before activation
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...logging_utils import get_component_logger
from ...vector_migrations.coordinator import VectorMigrationCoordinator
from ...vector_migrations.store import MigrationStatus
from ..deps import current_settings

logger = get_component_logger("api.vector_migrations")

router = APIRouter(prefix="/api/vector-databases/migrate", tags=["vector-migrations"])

# Process-level coordinator singleton (keyed by data_dir)
_coordinators: dict[str, VectorMigrationCoordinator] = {}


def _get_coordinator() -> VectorMigrationCoordinator:
    settings = current_settings()
    key = str(settings.data_dir)
    coord = _coordinators.get(key)
    if coord is None:
        coord = VectorMigrationCoordinator(settings)
        _coordinators[key] = coord
    return coord


class MigrationStatusResponse(BaseModel):
    id: str | None = None
    source_profile_id: str | None = None
    target_profile_id: str | None = None
    status: str | None = None
    phase: str | None = None
    kb_total: int = 0
    kb_copied: int = 0
    memory_total: int = 0
    memory_copied: int = 0
    heartbeat_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


def _to_response(mig) -> MigrationStatusResponse:
    if mig is None:
        return MigrationStatusResponse()
    return MigrationStatusResponse(
        id=mig.id,
        source_profile_id=mig.source_profile_id,
        target_profile_id=mig.target_profile_id,
        status=mig.status,
        phase=mig.phase,
        kb_total=mig.kb_total,
        kb_copied=mig.kb_copied,
        memory_total=mig.memory_total,
        memory_copied=mig.memory_copied,
        heartbeat_at=mig.heartbeat_at,
        started_at=mig.started_at,
        finished_at=mig.finished_at,
        error=mig.error,
    )


@router.post("", response_model=MigrationStatusResponse)
def start_migration():
    """Start migrating from the active profile to the pending profile."""
    coord = _get_coordinator()
    try:
        mig = coord.submit()
        return _to_response(mig)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("", response_model=MigrationStatusResponse)
def get_migration_status():
    """Return the current or latest migration status."""
    coord = _get_coordinator()
    return _to_response(coord.get_status())


@router.post("/retry", response_model=MigrationStatusResponse)
def retry_migration():
    """Retry a failed, interrupted, or cancelled migration."""
    coord = _get_coordinator()
    mig = coord.get_status()
    if mig is not None and mig.status == MigrationStatus.RUNNING:
        raise HTTPException(status_code=409, detail="migration already running")
    try:
        new_mig = coord.submit()
        return _to_response(new_mig)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/cancel", response_model=MigrationStatusResponse)
def cancel_migration():
    """Cancel the running migration (before activation phase only)."""
    coord = _get_coordinator()
    try:
        coord.cancel()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_response(coord.get_status())
