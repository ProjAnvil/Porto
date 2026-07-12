from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ...agent import PortoAgent
from ...documents import read_document
from ...llm import LLMClient
from ...logging_utils import get_component_logger
from ...models import WorkflowRequest, WorkflowResponse
from ..deps import apply_rag_settings, effective_rag_settings, get_memory, get_store

logger = get_component_logger("api")

router = APIRouter()


@router.post("/api/porto/workflows", response_model=WorkflowResponse)
def run_porto_workflow(req: WorkflowRequest):
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    logger.info(
        "workflow start session_id=%s text_chars=%s top_k=%s",
        req.session_id,
        len(req.text),
        req.top_k,
    )
    rag_settings = effective_rag_settings(req.rag)
    top_k = req.top_k or rag_settings.top_k
    runtime_settings = apply_rag_settings(req.rag, agent=req.agent, top_k=top_k)
    get_memory(runtime_settings).add(
        session_id=req.session_id,
        role="user",
        content=req.text[:2000],
        metadata={"kind": "workflow_request"},
    )
    agent = PortoAgent(runtime_settings, get_store(runtime_settings), LLMClient(runtime_settings))
    response = agent.run(req.text, project_name=req.project_name, top_k=top_k)
    get_memory(runtime_settings).add(
        session_id=req.session_id,
        role="assistant",
        content=f"Workflow {response.workflow_id}: {', '.join(s.name for s in response.subsystems)}",
        metadata={"kind": "workflow_response", "workflow_id": response.workflow_id},
    )
    logger.info(
        "workflow finish session_id=%s workflow_id=%s subsystems=%s",
        req.session_id,
        response.workflow_id,
        len(response.subsystems),
    )
    return response


@router.post("/api/porto/workflows/upload", response_model=WorkflowResponse)
async def run_porto_workflow_upload(
    file: Annotated[UploadFile, File()],
    project_name: Annotated[str | None, Form()] = None,
    top_k: Annotated[int | None, Form()] = None,
):
    suffix = Path(file.filename or "").suffix
    if not suffix:
        raise HTTPException(status_code=400, detail="uploaded file must have an extension")
    logger.info("workflow upload start filename=%s suffix=%s top_k=%s", file.filename, suffix, top_k)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        text = read_document(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if not text.strip():
        raise HTTPException(status_code=400, detail="document has no extractable text")
    rag_settings = effective_rag_settings()
    resolved_top_k = top_k or rag_settings.top_k
    runtime_settings = apply_rag_settings(top_k=resolved_top_k)
    agent = PortoAgent(runtime_settings, get_store(runtime_settings), LLMClient(runtime_settings))
    response = agent.run(text, project_name=project_name, top_k=resolved_top_k)
    logger.info(
        "workflow upload finish filename=%s workflow_id=%s text_chars=%s",
        file.filename,
        response.workflow_id,
        len(text),
    )
    return response
