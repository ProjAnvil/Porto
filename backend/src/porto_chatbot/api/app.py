from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ..agent_sdk.skills import deploy_skills
from ..logging_utils import get_component_logger, setup_logging
from ..settings import settings
from .deps import get_health_monitor, get_index_supervisor, get_workflow_executor
from .routes import (
    chat,
    knowledge,
    memory,
    workflow,
)
from .routes import (
    eval as eval_routes,
)
from .routes import (
    settings as settings_routes,
)

logger = get_component_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动常驻线程：IndexSupervisor（唯一 reindex 执行者）+ HealthMonitor。

    supervisor.start() 仅清理上次崩溃残留（running→interrupted），**不自动重建**；
    重建始终由用户手动触发。关闭时优雅停止两个 daemon。
    """
    # Re-assert logging configuration after uvicorn starts so that the
    # InterceptHandler overrides any handlers uvicorn installs, ensuring
    # all log records (including uvicorn's own) flow through loguru sinks.
    setup_logging(settings)
    # Deploy Agent SDK skills (idempotent, overwrites each startup).
    # Code is the source of truth; SKILL.md / CLAUDE.md are generated products.
    deploy_skills(settings.data_dir)
    supervisor = get_index_supervisor()
    health = get_health_monitor()
    supervisor.start()
    health.start()
    n = get_workflow_executor().recover_on_startup()
    if n:
        logger.info("workflow startup recovery: %s running workflows recovered", n)
    logger.info("lifespan startup: index supervisor + health monitor started")
    try:
        yield
    finally:
        supervisor.stop()
        health.stop()
        logger.info("lifespan shutdown: index supervisor + health monitor stopped")


app = FastAPI(title="Porto Chatbot API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    logger.info("request start method=%s path=%s", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.exception(
            "request failed method=%s path=%s elapsed_ms=%s",
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info(
        "request finish method=%s path=%s status=%s elapsed_ms=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


app.include_router(settings_routes.router)
app.include_router(knowledge.router)
app.include_router(chat.router)
app.include_router(workflow.router)
app.include_router(memory.router)
app.include_router(eval_routes.router)

# 捆绑部署：若前端静态导出产物存在（`npm run build:static` 后拷贝到 static_dir），
# 则由后端同源托管，浏览器直接以相对路径访问 /api/*，无需单独的 Node 进程或反代。
# 挂载在所有 /api 路由之后，保证 API 优先匹配。
if settings.static_dir.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=settings.static_dir, html=True),
        name="frontend",
    )
    logger.info("serving bundled frontend from %s", settings.static_dir)
