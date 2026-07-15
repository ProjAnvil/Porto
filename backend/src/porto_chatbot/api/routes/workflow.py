"""Workflow API —— 异步分步推进 + checkpoint 编辑/回退。

7 endpoints(顺序与 spec 2.1 对应):
- POST   /api/porto/workflows                       创建 + 后台启动 workflow
- POST   /api/porto/workflows/upload                上传文件 → 创建 + 启动
- GET    /api/porto/workflows                       列表(可按 session_id/status 过滤)
- GET    /api/porto/workflows/{id}                  详情(含各步 outputs)
- POST   /api/porto/workflows/{id}/advance          推进到下个 checkpoint
- PUT    /api/porto/workflows/{id}/steps/{step}     覆盖某步产出(用户编辑)+ 回退
- DELETE /api/porto/workflows/{id}                  删除

创建/上传/advance 返回精简的 {workflow_id, status:"running"},详情/PUT 返回 WorkflowDetail。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ...documents import read_document
from ...logging_utils import get_component_logger
from ...models import WorkflowRequest
from ..deps import (
    effective_agent_settings,
    effective_rag_settings,
    get_workflow_executor,
    get_workflow_store,
)

logger = get_component_logger("api")

router = APIRouter()

#: 用户可编辑产出的 checkpoint 步骤白名单(PUT /steps/{step} 仅接受这三者)。
#: retrieve/evaluate 不暴露给前端编辑(retrieve 由系统驱动;evaluate 是终态结论)。
_EDITABLE_STEPS = {"understand", "identify", "generate"}


class WorkflowCreated(BaseModel):
    workflow_id: str
    status: str


class WorkflowListItem(BaseModel):
    workflow_id: str
    session_id: str
    project_name: str | None
    status: str
    current_step: str | None
    created_at: str
    score: int | None = None


class WorkflowListResponse(BaseModel):
    items: list[WorkflowListItem]
    total: int
    has_more: bool


class WorkflowDetail(BaseModel):
    workflow_id: str
    session_id: str
    project_name: str | None
    status: str
    current_step: str | None
    error: str | None
    created_at: str
    updated_at: str
    outputs: dict[str, Any]  # {step: {output, produced_by, produced_at}}


class SpecUpdateRequest(BaseModel):
    name: str
    body: str


def _detail(store, workflow_id: str) -> WorkflowDetail:
    row = store.get(workflow_id)
    if row is None:
        raise HTTPException(404, "workflow not found")
    outs = store.get_outputs(workflow_id)
    return WorkflowDetail(
        workflow_id=row["workflow_id"],
        session_id=row["session_id"],
        project_name=row["project_name"],
        status=row["status"],
        current_step=row["current_step"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        outputs={
            k: {
                "output": v["output"],
                "produced_by": v["produced_by"],
                "produced_at": v["produced_at"],
            }
            for k, v in outs.items()
        },
    )


@router.post("/api/porto/workflows", response_model=WorkflowCreated)
def create_workflow(req: WorkflowRequest):
    """创建 workflow 并后台启动(executor.start_workflow 异步跑到 understand checkpoint)。

    立即返回 {workflow_id, status:"running"};前端轮询 GET /workflows/{id} 拿详情。
    """
    if not req.text or not req.text.strip():
        raise HTTPException(400, "text is required")
    rag = effective_rag_settings(req.rag).model_dump(exclude_none=True)
    agent = effective_agent_settings(req.agent).model_dump(exclude_none=True)
    top_k = req.top_k or effective_rag_settings(req.rag).top_k
    store = get_workflow_store()
    wid = store.create(
        req.session_id, req.project_name, req.text.strip(), top_k, rag, agent
    )
    logger.info(
        "workflow start session_id=%s workflow_id=%s text_chars=%s top_k=%s",
        req.session_id,
        wid,
        len(req.text or ""),
        top_k,
    )
    get_workflow_executor().start_workflow(wid)
    return WorkflowCreated(workflow_id=wid, status="running")


@router.post("/api/porto/workflows/upload", response_model=WorkflowCreated)
async def upload_workflow(
    file: Annotated[UploadFile, File()],
    project_name: Annotated[str | None, Form()] = None,
    session_id: Annotated[str | None, Form()] = "default",
    top_k: Annotated[int | None, Form()] = None,
):
    """上传文档 → 抽取文本 → 创建 + 启动 workflow。"""
    suffix = Path(file.filename or "").suffix
    if not suffix:
        raise HTTPException(400, "uploaded file must have an extension")
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        text = read_document(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if not text.strip():
        raise HTTPException(400, "document has no extractable text")
    rag = effective_rag_settings().model_dump(exclude_none=True)
    agent = effective_agent_settings().model_dump(exclude_none=True)
    resolved_top_k = top_k or effective_rag_settings().top_k
    store = get_workflow_store()
    sid = session_id or "default"
    wid = store.create(sid, project_name, text.strip(), resolved_top_k, rag, agent)
    logger.info(
        "workflow upload start filename=%s workflow_id=%s text_chars=%s",
        file.filename,
        wid,
        len(text),
    )
    get_workflow_executor().start_workflow(wid)
    return WorkflowCreated(workflow_id=wid, status="running")


@router.get("/api/porto/workflows", response_model=WorkflowListResponse)
def list_workflows(
    session_id: str | None = None,
    status: str | None = None,
    date: str | None = None,
    limit: int = 20,
    offset: int = 0,
):
    """列表(按 created_at DESC),可按 session_id / status / date 过滤,分页。

    每条附 evaluation score(若有)——list 不展开完整 outputs,避免大 payload。
    """
    store = get_workflow_store()
    rows, total = store.list_workflows(
        session_id=session_id, status=status, date=date, limit=limit, offset=offset
    )
    items: list[WorkflowListItem] = []
    for r in rows:
        score = None
        outs = store.get_outputs(r["workflow_id"])
        if "evaluate" in outs:
            score = (outs["evaluate"]["output"].get("evaluation") or {}).get("score")
        items.append(
            WorkflowListItem(
                workflow_id=r["workflow_id"],
                session_id=r["session_id"],
                project_name=r["project_name"],
                status=r["status"],
                current_step=r["current_step"],
                created_at=r["created_at"],
                score=score,
            )
        )
    return WorkflowListResponse(
        items=items, total=total, has_more=offset + len(items) < total
    )


@router.get("/api/porto/workflows/{workflow_id}", response_model=WorkflowDetail)
def get_workflow(workflow_id: str):
    """详情(含各步 outputs 与 produced_by 审计字段)。"""
    return _detail(get_workflow_store(), workflow_id)


@router.post(
    "/api/porto/workflows/{workflow_id}/advance", response_model=WorkflowCreated
)
def advance_workflow(workflow_id: str):
    """推进到下个 checkpoint。

    - 404: workflow 不存在
    - 409: workflow 已 completed,或 executor 返回 False(当前正在 running)
    - 200: 已接受,后台 worker 已启动
    """
    store = get_workflow_store()
    row = store.get(workflow_id)
    if row is None:
        raise HTTPException(404, "workflow not found")
    if row["status"] == "completed":
        raise HTTPException(409, "workflow already completed")
    if not get_workflow_executor().advance(workflow_id):
        raise HTTPException(409, "workflow is currently running")
    return WorkflowCreated(workflow_id=workflow_id, status="running")


@router.put(
    "/api/porto/workflows/{workflow_id}/steps/{step}", response_model=WorkflowDetail
)
def save_step_output(workflow_id: str, step: str, body: dict[str, Any]):
    """覆盖某步产出并回退到该步(用户编辑)。

    语义:
    1. save_output(step, body, produced_by="user") —— 覆盖该步既有产出。
    2. clear_outputs_after(step) —— 该步之后的产出全部清空(下游需重算)。
    3. current_step=step + status=awaiting_input —— workflow 退回该 checkpoint,
       下次 advance 从 step 的下一步重新跑。

    step 必须在 {understand, identify, generate},否则 400。
    """
    store = get_workflow_store()
    if store.get(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    if step not in _EDITABLE_STEPS:
        raise HTTPException(400, "step is not editable")
    store.save_output(workflow_id, step, body, "user")
    store.clear_outputs_after(workflow_id, step)
    store.update_status(workflow_id, "awaiting_input", current_step=step)
    return _detail(store, workflow_id)


@router.patch(
    "/api/porto/workflows/{workflow_id}/specs", response_model=WorkflowDetail
)
def update_spec(workflow_id: str, payload: SpecUpdateRequest):
    """轻量更新某个 spec 正文：只改 generate.output.specs[name]，
    不动审计字段、不清下游、不改 status/current_step。
    workflow 不存在→404；无 generate output 或 name 不在 specs→400。"""
    store = get_workflow_store()
    if store.get(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    if not store.update_spec(workflow_id, payload.name, payload.body):
        raise HTTPException(400, "spec not found")
    return _detail(store, workflow_id)


@router.delete("/api/porto/workflows/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: str):
    """删除 workflow + 其 outputs。无响应体(204)。"""
    store = get_workflow_store()
    if store.get(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    store.delete(workflow_id)
