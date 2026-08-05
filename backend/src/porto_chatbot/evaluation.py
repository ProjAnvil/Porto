from __future__ import annotations

from .embeddings import local_embed_text
from .logging_utils import get_component_logger
from .models import EvalCase, Subsystem
from .models.evaluation import (
    RagBatchEvaluation,
    RagCaseEvaluation,
    RagMetrics,
    WorkflowCheck,
    WorkflowEvaluation,
)
from .vector_store import cosine

logger = get_component_logger("evaluation")


def evaluate_workflow(
    prd_text: str,
    understanding: str,
    subsystems: list[Subsystem],
    specs: dict[str, str],
) -> WorkflowEvaluation:
    logger.info(
        "workflow evaluation start prd_chars=%s understanding_chars=%s subsystems=%s specs=%s",
        len(prd_text),
        len(understanding),
        len(subsystems),
        len(specs),
    )
    checks = [
        WorkflowCheck(
            name="understanding_non_empty",
            passed=len(understanding.strip()) >= 120,
            weight=20,
        ),
        WorkflowCheck(
            name="subsystems_identified",
            passed=len(subsystems) >= 1,
            weight=20,
        ),
        WorkflowCheck(
            name="subsystem_fields_complete",
            passed=all(s.name and s.responsibility and s.capabilities for s in subsystems),
            weight=20,
        ),
        WorkflowCheck(
            name="spec_per_subsystem",
            passed=set(specs) == {s.name for s in subsystems},
            weight=20,
        ),
        WorkflowCheck(
            name="spec_sections_present",
            passed=all("API 需求" in spec and "数据模型需求" in spec for spec in specs.values()),
            weight=20,
        ),
    ]
    score = sum(c.weight for c in checks if c.passed)
    result = WorkflowEvaluation(
        score=score,
        passed=score >= 80,
        checks=checks,
        prd_chars=len(prd_text),
    )
    logger.info("workflow evaluation finish score=%s passed=%s", result.score, result.passed)
    return result


def evaluate_rag_case(case: EvalCase) -> RagCaseEvaluation:
    logger.info(
        "rag evaluation case start question_chars=%s contexts=%s answer_chars=%s",
        len(case.question),
        len(case.contexts),
        len(case.answer),
    )
    answer = case.answer.strip()
    contexts = [c.strip() for c in case.contexts if c.strip()]
    context_text = "\n".join(contexts)
    question_vec = local_embed_text(case.question)
    answer_vec = local_embed_text(answer)
    context_vec = local_embed_text(context_text) if context_text else [0.0] * len(question_vec)

    answer_relevance = cosine(question_vec, answer_vec)
    context_relevance = cosine(question_vec, context_vec) if contexts else 0.0
    groundedness = _lexical_overlap(answer, context_text) if contexts else 0.0
    faithfulness = _sentence_support(answer, contexts) if contexts else 0.0

    score = round(
        100
        * (
            0.30 * max(0.0, answer_relevance)
            + 0.25 * max(0.0, context_relevance)
            + 0.25 * groundedness
            + 0.20 * faithfulness
        ),
        2,
    )
    result = RagCaseEvaluation(
        question=case.question,
        score=score,
        passed=score >= 55,
        metrics=RagMetrics(
            answer_relevance=round(answer_relevance, 4),
            context_relevance=round(context_relevance, 4),
            groundedness=round(groundedness, 4),
            faithfulness=round(faithfulness, 4),
        ),
        notes="Lightweight local eval inspired by RAGAS dimensions; no judge LLM required.",
    )
    logger.info("rag evaluation case finish score=%s passed=%s", result.score, result.passed)
    return result


def evaluate_rag_cases(cases: list[EvalCase]) -> RagBatchEvaluation:
    logger.info("rag evaluation start cases=%s", len(cases))
    results = [evaluate_rag_case(case) for case in cases]
    avg = round(sum(r.score for r in results) / len(results), 2) if results else 0.0
    result = RagBatchEvaluation(score=avg, passed=avg >= 55, cases=results)
    logger.info("rag evaluation finish score=%s passed=%s", result.score, result.passed)
    return result


def _lexical_overlap(answer: str, context: str) -> float:
    from .embeddings import tokens

    answer_tokens = set(tokens(answer))
    context_tokens = set(tokens(context))
    if not answer_tokens:
        return 0.0
    return len(answer_tokens & context_tokens) / len(answer_tokens)


def _sentence_support(answer: str, contexts: list[str]) -> float:
    sentences = [s.strip() for s in answer.replace("。", ".").split(".") if s.strip()]
    if not sentences:
        return 0.0
    supported = 0
    context = "\n".join(contexts)
    for sentence in sentences:
        if _lexical_overlap(sentence, context) >= 0.35:
            supported += 1
    return supported / len(sentences)
