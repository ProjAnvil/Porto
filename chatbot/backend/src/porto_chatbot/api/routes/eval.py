from __future__ import annotations

from fastapi import APIRouter

from ...evaluation import evaluate_rag_cases
from ...logging_utils import get_component_logger
from ...models import EvalRequest

logger = get_component_logger("api")

router = APIRouter()


@router.post("/api/eval/rag")
def evaluate_rag(req: EvalRequest):
    logger.info("eval rag cases=%s", len(req.cases))
    return evaluate_rag_cases(req.cases)
