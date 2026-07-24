from __future__ import annotations

from ...evaluation import evaluate_workflow


def evaluate(state, *, config):
    agent = config["configurable"]["agent"]
    agent.logger.info("step evaluate start workflow_id=%s", state["workflow_id"])
    evaluation = evaluate_workflow(
        state["prd_text"],
        state["understanding"],
        state["subsystems"],
        state["specs"],
    )
    # 聚合 spec loop 的 rubric 分数（若启用 LLM loop）
    spec_results = state.get("spec_results") or {}
    rubric_scores = [r.attempts[-1].score for r in spec_results.values() if r.attempts]
    if rubric_scores:
        evaluation["spec_rubric_avg"] = round(sum(rubric_scores) / len(rubric_scores), 2)
        evaluation["spec_rubric_min"] = min(rubric_scores)

    # 条件回边决策
    passes = int(state.get("rework_passes", 0))
    below_bar = (
        not evaluation.get("passed", True)
        or evaluation.get("spec_rubric_min", agent.settings.spec_refine_pass_score)
        < agent.settings.spec_refine_pass_score
    )
    needs_rework = (
        agent.settings.workflow_rework_enabled
        and below_bar
        and passes < agent.settings.workflow_rework_max_passes
    )
    agent.logger.info(
        "step evaluate finish score=%s rubric_avg=%s needs_rework=%s passes=%s",
        evaluation.get("score"),
        evaluation.get("spec_rubric_avg"),
        needs_rework,
        passes,
    )
    return {
        "evaluation": evaluation,
        "rework_passes": passes + 1 if needs_rework else passes,
        "needs_rework": needs_rework,
        "current_step": "evaluate",
        **agent._step("evaluate", f"评估得分 {evaluation['score']}", evaluation),
    }
