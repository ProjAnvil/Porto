"""Unit + integration tests for specs/subgraph.py.

- Unit: the four branches of ``_should_stop`` + the ``init_spec`` fallback path (M8)
- Integration: actually run the subgraph (mock LLM), 2 subsystems → ``spec_results`` has
  2 keys with complete SpecResult fields (verifies the ``_dict_merge`` reducer merge, audit
  blocker B1)
"""

from __future__ import annotations

from typing import Any

from porto_chatbot.agent.state import _dict_merge
from porto_chatbot.llm import LLMClient, ToolLoopResult
from porto_chatbot.models import SpecAttempt, SpecResult, Subsystem
from porto_chatbot.models.enums import SpecVerdict
from porto_chatbot.settings import Settings
from porto_chatbot.specs.subgraph import (
    SpecSubgraphState,
    _should_stop,
    build_spec_subgraph,
    init_spec,
)

# ----------------------------- helpers ----------------------------- #


def _settings(tmp_path, **overrides) -> Settings:
    s = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        agent_provider="openai",
        agent_model="m",
    )
    s.agent_api_key = "k"
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def _sub(name: str = "billing-service") -> Subsystem:
    return Subsystem(name=name, responsibility="账单管理", capabilities=["出账"], data_entities=["Bill"])


def _make_llm(
    settings: Settings,
    *,
    cwt_texts: list[str] | None = None,
    critiques: list[Any] | None = None,
    refine_texts: list[str] | None = None,
) -> LLMClient:
    """Build an LLMClient with complete_with_tools / complete_structured / complete mocked.

    generate_initial_spec goes through ctx.backend.execute_node(tools=...) →
    llm.complete_with_tools; critique_spec through
    ctx.backend.execute_node(structured_schema=...) → llm.complete_structured; refine_spec
    through ctx.backend.execute_node() → llm.complete.
    SpecContext.__post_init__ wraps a LangchainBackend automatically, so ctx_backend can be
    left out.
    """
    llm = LLMClient(settings)
    cwt_iter = iter(cwt_texts or [])
    llm.complete_with_tools = lambda *a, **k: ToolLoopResult(text=next(cwt_iter, ""))
    crit_iter = iter(critiques or [])
    llm.complete_structured = lambda *a, **k: next(crit_iter, None)
    refine_iter = iter(refine_texts or [])
    llm.complete = lambda *a, **k: next(refine_iter, "")
    return llm


def _make_state(
    tmp_path,
    *,
    sub: Subsystem | None = None,
    cwt_texts: list[str] | None = None,
    critiques: list[Any] | None = None,
    refine_texts: list[str] | None = None,
    **settings_kw,
) -> dict:
    """Build the subgraph input state (SpecContext expanded into ctx_* fields)."""
    settings = _settings(tmp_path, **settings_kw)
    llm = _make_llm(
        settings, cwt_texts=cwt_texts, critiques=critiques, refine_texts=refine_texts
    )
    return {
        "sub": sub or _sub(),
        "ctx_llm": llm,
        "ctx_settings": settings,
        "ctx_state": {"workflow_id": "w1", "sources": [], "understanding": "理解"},
        "ctx_file_service": None,
    }


def _crit(verdict: str, score: int, feedback: str = "fix") -> dict:
    return {"verdict": verdict, "score": score, "feedback": feedback, "per_dimension": {}}


def _ctx_settings(tmp_path, **overrides) -> tuple[Settings, dict]:
    """Build a minimal set of ctx_* fields for the _should_stop / init_spec unit tests."""
    settings = _settings(tmp_path, **overrides)
    llm = LLMClient(settings)
    return settings, {
        "ctx_llm": llm,
        "ctx_settings": settings,
        "ctx_state": {"workflow_id": "w1", "sources": [], "understanding": "理解"},
    }


# -------------------------- Unit: init_spec fallback -------------------------- #


def test_init_spec_disabled_llm_uses_template(tmp_path):
    """LLM disabled → template fallback: best and current are both the template,
    truncated=False."""
    settings, ctx_fields = _ctx_settings(tmp_path, agent_api_key=None)
    state: SpecSubgraphState = {"sub": _sub(), **ctx_fields}  # type: ignore[typeddict-item]
    result = init_spec(state)
    assert "系统需求" in result["current_spec"]
    assert result["best_spec"] == result["current_spec"]
    assert result["best_score"] == -1
    assert result["iteration"] == 0
    assert result["truncated"] is False
    assert result["attempts"] == []
    assert result["tool_meta"] == {}


def test_init_spec_refine_disabled_uses_template(tmp_path):
    """spec_refine_enabled=False → takes the same template fallback path."""
    settings, ctx_fields = _ctx_settings(tmp_path, spec_refine_enabled=False)
    state: SpecSubgraphState = {"sub": _sub(), **ctx_fields}  # type: ignore[typeddict-item]
    result = init_spec(state)
    assert "系统需求" in result["current_spec"]
    assert result["truncated"] is False


# ------------------------- Unit: _should_stop branches ------------------------- #


def test_should_stop_pass_verdict_goes_finalize(tmp_path):
    """1) verdict == PASS → finalize."""
    settings, ctx_fields = _ctx_settings(tmp_path)
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "iteration": 1,
        "attempts": [SpecAttempt(version=1, score=10, verdict=SpecVerdict.PASS)],
        "best_score": 10,
        "used_chars": 100,
        "best_improved": True,
    }
    assert _should_stop(state) == "finalize"


def test_should_stop_score_meets_pass_score(tmp_path):
    """1) score >= pass_score -> finalize, even when verdict is not PASS.

    A 7th rubric dimension moved the max from 12 to 14 and the default pass score
    from 10 to 12, so read the default instead of hardcoding it.
    """
    settings, ctx_fields = _ctx_settings(tmp_path)
    passing = settings.spec_refine_pass_score
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "iteration": 1,
        "attempts": [
            SpecAttempt(version=1, score=passing, verdict=SpecVerdict.NEEDS_IMPROVEMENT)
        ],
        "best_score": passing,
        "used_chars": 100,
        "best_improved": True,
    }
    assert _should_stop(state) == "finalize"


def test_should_stop_max_iter(tmp_path):
    """2) iteration >= max_iter → finalize."""
    settings, ctx_fields = _ctx_settings(tmp_path)
    # max_iter defaults to 3; build a case that has not passed yet but has hit the limit
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "iteration": settings.spec_refine_max_iter,
        "attempts": [
            SpecAttempt(version=1, score=5, verdict=SpecVerdict.NEEDS_IMPROVEMENT),
            SpecAttempt(version=2, score=7, verdict=SpecVerdict.NEEDS_IMPROVEMENT),
            SpecAttempt(version=3, score=8, verdict=SpecVerdict.NEEDS_IMPROVEMENT),
        ],
        "best_score": 8,
        "used_chars": 100,
        "best_improved": True,  # still improved this round, but max_iter is reached
    }
    assert _should_stop(state) == "finalize"


def test_should_stop_score_degradation(tmp_path):
    """3) This round's score did not beat best (flat or lower) → finalize."""
    settings, ctx_fields = _ctx_settings(tmp_path)
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "iteration": 2,
        "attempts": [
            SpecAttempt(version=1, score=8, verdict=SpecVerdict.NEEDS_IMPROVEMENT),
            SpecAttempt(version=2, score=5, verdict=SpecVerdict.NEEDS_IMPROVEMENT),
        ],
        "best_score": 8,  # this round 5 < previous 8 → best_improved=False
        "used_chars": 100,
        "best_improved": False,
    }
    assert _should_stop(state) == "finalize"


def test_should_stop_score_plateau_counts_as_degradation(tmp_path):
    """3) A flat score also counts as regressing (same as ``score <= best_score`` in
    loop.py)."""
    settings, ctx_fields = _ctx_settings(tmp_path)
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "iteration": 2,
        "attempts": [
            SpecAttempt(version=1, score=7, verdict=SpecVerdict.NEEDS_IMPROVEMENT),
            SpecAttempt(version=2, score=7, verdict=SpecVerdict.NEEDS_IMPROVEMENT),
        ],
        "best_score": 7,
        "used_chars": 100,
        "best_improved": False,  # flat → best not updated → regression
    }
    assert _should_stop(state) == "finalize"


def test_should_stop_budget_exceeded(tmp_path):
    """4) used_chars > budget*4 → finalize."""
    settings, ctx_fields = _ctx_settings(tmp_path, spec_refine_budget_tokens=1)
    # budget_tokens=1 → budget_chars=4, and used_chars=5000 is far above it
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "iteration": 1,
        "attempts": [SpecAttempt(version=1, score=5, verdict=SpecVerdict.NEEDS_IMPROVEMENT)],
        "best_score": 5,
        "used_chars": 5000,
        "best_improved": True,
    }
    assert _should_stop(state) == "finalize"


def test_should_stop_continues_to_refine(tmp_path):
    """None of the four conditions holds → refine."""
    settings, ctx_fields = _ctx_settings(tmp_path)
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "iteration": 1,  # < max_iter=3
        "attempts": [SpecAttempt(version=1, score=5, verdict=SpecVerdict.NEEDS_IMPROVEMENT)],
        "best_score": 5,  # this round 5 > previous -1 → improvement
        "used_chars": 100,  # far below the budget
        "best_improved": True,
    }
    assert _should_stop(state) == "refine"


def test_should_stop_truncated_short_circuits(tmp_path):
    """Tool truncation → finalize right away, no critique."""
    _, ctx_fields = _ctx_settings(tmp_path)
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "truncated": True,
        "iteration": 0,
        "attempts": [],
    }
    assert _should_stop(state) == "finalize"


def test_should_stop_critic_failed(tmp_path):
    """Critic unavailable → finalize (the current spec is accepted)."""
    settings, ctx_fields = _ctx_settings(tmp_path)
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "iteration": 1,
        "attempts": [
            SpecAttempt(version=1, verdict=SpecVerdict.NEEDS_IMPROVEMENT, feedback_digest="critic 不可用"),
        ],
        "best_score": -1,
        "used_chars": 100,
        "critic_failed": True,
    }
    assert _should_stop(state) == "finalize"


# --------------------------- Integration: run the subgraph --------------------------- #


def test_subgraph_pass_on_first_critique(tmp_path):
    """PASS on the first round → spec_results has 1 key and attempts has length 1."""
    state = _make_state(
        tmp_path,
        cwt_texts=["INITIAL SPEC"],
        critiques=[_crit("PASS", 10)],
    )
    graph = build_spec_subgraph()
    result = graph.invoke(state)
    spec_results = result["spec_results"]
    assert set(spec_results) == {"billing-service"}
    sr: SpecResult = spec_results["billing-service"]
    assert sr.final == "INITIAL SPEC"
    assert sr.iterations == 1
    assert sr.truncated is False
    assert sr.used_llm is True
    assert len(sr.attempts) == 1
    assert sr.attempts[0].verdict == SpecVerdict.PASS
    assert sr.attempts[0].score == 10


def test_subgraph_refines_until_pass(tmp_path):
    """Loops critique → refine → critique until PASS."""
    state = _make_state(
        tmp_path,
        cwt_texts=["V0"],
        critiques=[_crit("NEEDS_IMPROVEMENT", 5, "add error codes"), _crit("PASS", 11)],
        refine_texts=["V1"],
    )
    graph = build_spec_subgraph()
    result = graph.invoke(state)
    sr: SpecResult = result["spec_results"]["billing-service"]
    assert sr.final == "V1"
    assert sr.iterations == 2
    assert sr.truncated is False
    assert [a.score for a in sr.attempts] == [5, 11]
    assert [a.version for a in sr.attempts] == [1, 2]


def test_subgraph_max_iter_truncation(tmp_path):
    """Never reaches the bar → stops at max_iter, truncated=True."""
    state = _make_state(
        tmp_path,
        cwt_texts=["V0"],
        critiques=[
            _crit("NEEDS_IMPROVEMENT", 3),
            _crit("NEEDS_IMPROVEMENT", 5),
            _crit("NEEDS_IMPROVEMENT", 7),
        ],
        refine_texts=["V1", "V2"],
        spec_refine_max_iter=3,
    )
    graph = build_spec_subgraph()
    result = graph.invoke(state)
    sr: SpecResult = result["spec_results"]["billing-service"]
    assert sr.truncated is True
    assert sr.iterations == 3
    assert sr.final == "V2"  # the highest-scoring version


def test_subgraph_degradation_keeps_best(tmp_path):
    """3) Regression → keeps best (the first version)."""
    state = _make_state(
        tmp_path,
        cwt_texts=["V0"],
        critiques=[_crit("NEEDS_IMPROVEMENT", 8), _crit("NEEDS_IMPROVEMENT", 5)],
        refine_texts=["V1"],
        spec_refine_max_iter=3,
    )
    graph = build_spec_subgraph()
    result = graph.invoke(state)
    sr: SpecResult = result["spec_results"]["billing-service"]
    assert sr.iterations == 2
    assert sr.truncated is False
    assert sr.final == "V0"  # falls back to best (the score-8 version)


def test_subgraph_budget_truncation(tmp_path):
    """4) Tiny budget → the budget truncation fires right after the first critique."""
    state = _make_state(
        tmp_path,
        cwt_texts=["V0"],
        critiques=[_crit("NEEDS_IMPROVEMENT", 5, "x" * 5000)],
        refine_texts=["V1"],
        spec_refine_max_iter=3,
        spec_refine_budget_tokens=1,  # a budget of 4 characters
    )
    graph = build_spec_subgraph()
    result = graph.invoke(state)
    sr: SpecResult = result["spec_results"]["billing-service"]
    assert sr.truncated is True
    assert sr.iterations == 1


def test_subgraph_critic_none_accepts_current(tmp_path):
    """critique_spec returns None → the current spec is accepted."""
    state = _make_state(
        tmp_path,
        cwt_texts=["V0"],
        critiques=[None],
    )
    graph = build_spec_subgraph()
    result = graph.invoke(state)
    sr: SpecResult = result["spec_results"]["billing-service"]
    assert sr.final == "V0"
    assert sr.iterations == 1
    assert "critic 不可用" in sr.attempts[0].feedback_digest


def test_subgraph_disabled_llm_uses_template(tmp_path):
    """LLM disabled → the whole subgraph takes the template fallback, used_llm=False."""
    state = _make_state(tmp_path, agent_api_key=None)
    graph = build_spec_subgraph()
    result = graph.invoke(state)
    sr: SpecResult = result["spec_results"]["billing-service"]
    assert sr.used_llm is False
    assert "系统需求" in sr.final
    assert sr.attempts == []
    assert sr.iterations == 0


def test_subgraph_generate_empty_falls_back_to_template(tmp_path):
    """generate_initial_spec returns empty → template fallback, and the critic still runs."""
    state = _make_state(
        tmp_path,
        cwt_texts=[""],  # generates an empty spec
        critiques=[_crit("PASS", 10)],
    )
    graph = build_spec_subgraph()
    result = graph.invoke(state)
    sr: SpecResult = result["spec_results"]["billing-service"]
    assert sr.used_llm is True
    assert "系统需求" in sr.final  # template fallback


def test_subgraph_tool_truncation_skips_critique(tmp_path):
    """Tool truncation → critique/refine are skipped and it finalizes right away."""
    settings = _settings(tmp_path)
    llm = LLMClient(settings)
    # generate_initial_spec goes through complete_with_tools; the truncation flag comes from
    # ToolLoopResult
    llm.complete_with_tools = lambda *a, **k: ToolLoopResult(
        text="⚠️ 规格生成超限",
        truncated=True,
        reason="tool_loop_truncated",
        turns=4,
        tool_calls=[{"name": "x"}],
    )
    state = {
        "sub": _sub(),
        "ctx_llm": llm,
        "ctx_settings": settings,
        "ctx_state": {"workflow_id": "w1", "sources": [], "understanding": "u"},
    }
    graph = build_spec_subgraph()
    result = graph.invoke(state)
    sr: SpecResult = result["spec_results"]["billing-service"]
    assert sr.attempts == []          # critique was skipped
    assert sr.iterations == 0
    assert sr.truncated is False       # a tool truncation does not set the refine truncated flag
    assert sr.used_llm is True
    assert sr.tool_meta["truncated"] is True
    assert sr.tool_meta["reason"] == "tool_loop_truncated"
    assert "规格生成未能完成" in sr.final  # steps.py reformats the truncation notice


# ------------------ Integration: multi-subsystem reducer merge (key for M8) ------------------ #


def test_subgraph_reducer_merges_two_subsystems(tmp_path):
    """Two subsystems each run the subgraph → _dict_merge merges spec_results and both keys
    are present.

    Regression test for audit item B1: if SpecSubgraphState does not declare
    ``spec_results: Annotated[dict, _dict_merge]``, a later write overwrites an earlier one
    when the parent graph merges and only one key is left. This test applies the reducer merge
    to the subgraph outputs directly and checks the data structure.
    """
    graph = build_spec_subgraph()

    # Subsystem 1: billing-service, PASS on the first round
    state_a = _make_state(
        tmp_path,
        sub=_sub("billing-service"),
        cwt_texts=["BILLING SPEC"],
        critiques=[_crit("PASS", 11)],
    )
    # Subsystem 2: payment-service, PASS after one refine
    state_b = _make_state(
        tmp_path,
        sub=_sub("payment-service"),
        cwt_texts=["PAY V0"],
        critiques=[_crit("NEEDS_IMPROVEMENT", 6, "add idempotency"), _crit("PASS", 10)],
        refine_texts=["PAY V1"],
    )

    result_a = graph.invoke(state_a)
    result_b = graph.invoke(state_b)

    # Emulate the reducer merge of the parent graph's Send fan-out
    merged_spec_results = _dict_merge(
        result_a.get("spec_results", {}),
        result_b.get("spec_results", {}),
    )

    # Both keys are present (the core assertion of audit blocker B1)
    assert set(merged_spec_results) == {"billing-service", "payment-service"}

    # Complete fields: SpecResult instances + the key fields
    billing: SpecResult = merged_spec_results["billing-service"]
    payment: SpecResult = merged_spec_results["payment-service"]
    assert isinstance(billing, SpecResult)
    assert isinstance(payment, SpecResult)
    assert billing.final == "BILLING SPEC"
    assert billing.iterations == 1
    assert billing.truncated is False
    assert billing.used_llm is True
    assert len(billing.attempts) == 1
    assert billing.attempts[0].verdict == SpecVerdict.PASS

    assert payment.final == "PAY V1"
    assert payment.iterations == 2
    assert payment.truncated is False
    assert [a.score for a in payment.attempts] == [6, 10]
    assert payment.tool_meta  # a non-tool truncation also has meta (turns / tool_calls, ...)


def test_subgraph_reducer_preserves_keys_across_many_subsystems(tmp_path):
    """5 subsystems → all 5 keys are present after the reducer (covers a fan-out closer to
    the real scale)."""
    graph = build_spec_subgraph()
    names = [f"svc-{i}" for i in range(5)]
    merged: dict[str, SpecResult] = {}
    for name in names:
        state = _make_state(
            tmp_path,
            sub=_sub(name),
            cwt_texts=[f"{name} spec"],
            critiques=[_crit("PASS", 12)],
        )
        result = graph.invoke(state)
        merged = _dict_merge(merged, result.get("spec_results", {}))
    assert set(merged) == set(names)
    for name in names:
        assert isinstance(merged[name], SpecResult)
        assert merged[name].final == f"{name} spec"
        assert merged[name].attempts[0].score == 12
