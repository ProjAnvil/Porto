from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .common import SourceChunk


class AgentStep(BaseModel):
    name: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class Subsystem(BaseModel):
    name: str
    type: Literal["new", "extend", "existing"] = "new"
    responsibility: str
    capabilities: list[str] = Field(default_factory=list)
    data_entities: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


class WorkflowResponse(BaseModel):
    workflow_id: str
    project_name: str
    understanding: str
    subsystems: list[Subsystem]
    specs: dict[str, str]
    evaluation: dict[str, Any]
    sources: list[SourceChunk]
    steps: list[AgentStep]
