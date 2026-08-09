"""Pydantic models for vector database profile requests and responses."""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..vector_stores import DistanceMetric
from .enums import VectorBackend


class AuthMode:
    """Authentication mode constants for vector profiles."""

    NONE = "none"
    TOKEN = "token"
    USER_PASSWORD = "user_password"


class VectorProfileDraft(BaseModel):
    """Request model for creating or updating a vector profile.

    Credentials are write-only: they are accepted here but never
    included in response models.
    """

    name: str = Field(..., min_length=3, max_length=50)
    backend: VectorBackend = VectorBackend.CHROMA
    metric: DistanceMetric = DistanceMetric.L2
    uri: str | None = None
    database: str | None = None
    auth_mode: str = AuthMode.NONE
    token: str | None = None
    username: str | None = None
    password: str | None = None
    tls: bool = False
    timeout: int = Field(default=30, ge=1, le=300)
    kb_collection_base: str = Field(..., min_length=3, max_length=50)
    memory_collection_base: str = Field(..., min_length=3, max_length=50)


class VectorProfileResponse(BaseModel):
    """Response model — credentials are never exposed, only ``credential_configured``."""

    id: str
    name: str
    backend: VectorBackend
    metric: DistanceMetric
    uri: str | None = None
    database: str | None = None
    auth_mode: str
    tls: bool
    timeout: int
    kb_collection_base: str
    memory_collection_base: str
    credential_configured: bool
    status: str
    created_at: str
    updated_at: str


class VectorStoreStateResponse(BaseModel):
    """Snapshot of the vector store selection state."""

    active: VectorProfileResponse | None = None
    pending: VectorProfileResponse | None = None
    revision: int = 1


class SaveVectorProfileRequest(VectorProfileDraft):
    """Save request — identical to draft, named for clarity in API routes."""


class SaveVectorProfileResponse(BaseModel):
    """Save response — includes whether migration is required."""

    profile: VectorProfileResponse
    migration_required: bool
