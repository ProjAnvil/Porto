"""Workflow API —— 异步分步推进 + checkpoint 编辑/回退。

8 endpoints(顺序与 spec 2.1 对应):
- POST   /api/porto/workflows                       创建 + 后台启动 workflow
- POST   /api/porto/workflows/upload                上传文件 → 创建 + 启动
- GET    /api/porto/workflows                       列表(可按 session_id/status 过滤)
- GET    /api/porto/workflows/{id}                  详情(含各步 outputs)
- POST   /api/porto/workflows/{id}/advance          推进到下个 checkpoint
- PUT    /api/porto/workflows/{id}/steps/{step}     覆盖某步产出(用户编辑)+ 回退
- POST   /api/porto/workflows/{id}/steps/{step}/rerun  整步重跑(turn ×1.5 cap hard_cap)
- DELETE /api/porto/workflows/{id}                  删除

创建/上传/advance/rerun 返回精简的 {workflow_id, status:"running"},详情/PUT 返回 WorkflowDetail。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ...agent.graph import STEPS
from ...documents import (
    SUPPORTED_EXTENSIONS,
    DocumentLimitError,
    DocumentNativeError,
    DocumentParseError,
)
from ...files.service import FileService
from ...llm import LLMClient
from ...logging_utils import get_component_logger
from ...models import WorkflowRequest
from ...models.enums import DocumentParseMode, WorkflowRunState
from ...workflow_executor import WorkflowRunning
from ..deps import (
    apply_rag_settings,
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
    status: WorkflowRunState


class WorkflowListItem(BaseModel):
    workflow_id: str
    session_id: str
    project_name: str | None
    status: WorkflowRunState
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
    status: WorkflowRunState
    current_step: str | None
    error: str | None
    created_at: str
    updated_at: str
    outputs: dict[str, Any]  # {step: {output, produced_by, produced_at}}


class SpecUpdateRequest(BaseModel):
    name: str
    body: str


class DocumentCapabilitiesView(BaseModel):
    enabled: bool
    image_input: bool
    native_pdf: bool
    reason: str
    parse_mode: DocumentParseMode


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
    wid = store.create(req.session_id, req.project_name, req.text.strip(), top_k, rag, agent)
    logger.info(
        "workflow start session_id=%s workflow_id=%s text_chars=%s top_k=%s",
        req.session_id,
        wid,
        len(req.text or ""),
        top_k,
    )
    get_workflow_executor().start_workflow(wid)
    return WorkflowCreated(workflow_id=wid, status=WorkflowRunState.RUNNING)


@router.post("/api/porto/workflows/upload", response_model=WorkflowCreated)
async def upload_workflow(
    file: Annotated[UploadFile, File()],
    project_name: Annotated[str | None, Form()] = None,
    session_id: Annotated[str | None, Form()] = "default",
    top_k: Annotated[int | None, Form()] = None,
):
    """上传文档 → 经 FileService 存储 → 创建 + 启动 workflow。

    Task 6:文档不再在路由层 parse,而是交给 FileService.store 落盘 + 解析 +
    分页。workflow 行只存 ``prd_file_id`` 引用,``prd_text`` 写空串占位
    (Task 7 之前节点仍读 state["prd_text"],该路径暂不可用 —— 见 Task 7)。
    """
    runtime_settings = apply_rag_settings()
    suffix = Path(file.filename or "").suffix.lower()
    if not suffix:
        raise HTTPException(400, "uploaded file must have an extension")
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(415, f"unsupported document type: {suffix}")
    # 大小检查保留在路由层:FileService.store 内部 read 无上限,需先 short-circuit
    # 避免把超大文件全部读进内存。
    max_bytes = runtime_settings.document_max_upload_mb * 1024 * 1024
    payload = await file.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise HTTPException(
            413,
            f"document exceeds {runtime_settings.document_max_upload_mb} MB upload limit",
        )
    await file.seek(0)

    sid = session_id or "default"
    file_svc = FileService(runtime_settings)
    try:
        meta = file_svc.store(file, owner_id=sid)
    except DocumentLimitError as exc:
        raise HTTPException(413, str(exc)) from exc
    except DocumentNativeError as exc:
        raise HTTPException(422, str(exc)) from exc
    except DocumentParseError as exc:
        raise HTTPException(400, str(exc)) from exc
    if meta.page_count == 0:
        raise HTTPException(
            400,
            "document has no extractable text; configure a PDF-capable vision model for scanned files",
        )

    rag = effective_rag_settings().model_dump(exclude_none=True)
    agent = effective_agent_settings().model_dump(exclude_none=True)
    resolved_top_k = top_k or effective_rag_settings().top_k
    store = get_workflow_store()
    wid = store.create(
        sid,
        project_name,
        "",  # prd_text 空串占位:实际内容经 FileService.read_pages(prd_file_id) 访问
        resolved_top_k,
        rag,
        agent,
        prd_file_id=meta.file_id,
    )
    logger.info(
        "workflow upload start filename=%s workflow_id=%s file_id=%s pages=%s",
        file.filename,
        wid,
        meta.file_id,
        meta.page_count,
    )
    get_workflow_executor().start_workflow(wid)
    return WorkflowCreated(workflow_id=wid, status=WorkflowRunState.RUNNING)


@router.get("/api/porto/document-capabilities", response_model=DocumentCapabilitiesView)
def document_capabilities():
    """Report effective model-side document capabilities without sending a file."""
    runtime = apply_rag_settings()
    capabilities = LLMClient(runtime).document_capabilities
    return DocumentCapabilitiesView(
        **capabilities.__dict__,
        parse_mode=runtime.document_parse_mode,
    )


@router.get("/api/porto/workflows", response_model=WorkflowListResponse)
def list_workflows(
    session_id: str | None = None,
    status: str | None = None,
    date: str | None = None,
    limit: int = 20,
    offset: int = 0,
):
    """列表(按 created_at DESC),可按 session_id / status / date 过滤,分页。

    Task 10:evaluate 节点已删,``score`` 字段保留为 None(向后兼容前端 schema),
    不再查询 outputs(原为 evaluate.output.evaluation.score)。
    """
    store = get_workflow_store()
    rows, total = store.list_workflows(
        session_id=session_id, status=status, date=date, limit=limit, offset=offset
    )
    items: list[WorkflowListItem] = [
        WorkflowListItem(
            workflow_id=r["workflow_id"],
            session_id=r["session_id"],
            project_name=r["project_name"],
            status=r["status"],
            current_step=r["current_step"],
            created_at=r["created_at"],
            score=None,
        )
        for r in rows
    ]
    return WorkflowListResponse(items=items, total=total, has_more=offset + len(items) < total)


@router.get("/api/porto/workflows/{workflow_id}", response_model=WorkflowDetail)
def get_workflow(workflow_id: str):
    """详情(含各步 outputs 与 produced_by 审计字段)。"""
    return _detail(get_workflow_store(), workflow_id)


@router.post("/api/porto/workflows/{workflow_id}/advance", response_model=WorkflowCreated)
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
    if row["status"] == WorkflowRunState.COMPLETED:
        raise HTTPException(409, "workflow already completed")
    if not get_workflow_executor().advance(workflow_id):
        raise HTTPException(409, "workflow is currently running") from None
    return WorkflowCreated(workflow_id=workflow_id, status=WorkflowRunState.RUNNING)


@router.post(
    "/api/porto/workflows/{workflow_id}/steps/{step}/rerun", response_model=WorkflowCreated
)
def rerun_step(workflow_id: str, step: str):
    """整步重跑:turn ×1.5(ceil) cap tool_turn_hard_cap,绕过 graph 调节点函数。

    - 404: workflow 不存在
    - 400: step 不在 STEPS
    - 409: 已达 turn 硬上限(引导手编)或 workflow 正在 running
    - 200: 已接受,后台 worker 重跑该步
    """
    store = get_workflow_store()
    if store.get(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    if step not in STEPS:
        raise HTTPException(400, f"step must be one of {STEPS}")
    try:
        get_workflow_executor().rerun_step(workflow_id, step)
    except WorkflowRunning:
        raise HTTPException(409, "workflow is running or turn limit reached") from None
    return WorkflowCreated(workflow_id=workflow_id, status=WorkflowRunState.RUNNING)


@router.put("/api/porto/workflows/{workflow_id}/steps/{step}", response_model=WorkflowDetail)
def save_step_output(workflow_id: str, step: str, body: dict[str, Any]):
    """覆盖某步产出并回退到该步(用户编辑)。

    executor.update_step:graph.update_state(as_node=step) 回退图位置 + 投影(edited→user)
    + 清下游 + status/current_step 同步。step 必须在 {understand, identify, generate}。
    """
    store = get_workflow_store()
    if store.get(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    if step not in _EDITABLE_STEPS:
        raise HTTPException(400, "step is not editable")
    try:
        get_workflow_executor().update_step(workflow_id, step, body)
    except WorkflowRunning:
        raise HTTPException(409, "workflow is currently running") from None
    return _detail(store, workflow_id)


@router.patch("/api/porto/workflows/{workflow_id}/specs", response_model=WorkflowDetail)
def update_spec(workflow_id: str, payload: SpecUpdateRequest):
    """轻量更新某个 spec 正文:executor.update_spec(审计 + graph state dict-merge),
    不动 status/current_step、不清下游、不改 produced_by。workflow 不存在→404;
    无 generate output 或 name 不在 specs→400。"""
    store = get_workflow_store()
    if store.get(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    try:
        if not get_workflow_executor().update_spec(workflow_id, payload.name, payload.body):
            raise HTTPException(400, "spec not found")
    except WorkflowRunning:
        raise HTTPException(409, "workflow is currently running") from None
    return _detail(store, workflow_id)


@router.delete("/api/porto/workflows/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: str):
    """删除 workflow + 其 outputs。无响应体(204)。"""
    store = get_workflow_store()
    if store.get(workflow_id) is None:
        raise HTTPException(404, "workflow not found")
    store.delete(workflow_id)
