from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from ..logging_utils import get_component_logger
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

app = FastAPI(title="Porto Chatbot API", version="0.1.0")
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
