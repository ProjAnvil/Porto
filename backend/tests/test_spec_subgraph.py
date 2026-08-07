"""specs/subgraph.py 的单元 + 集成测试。

- 单元：``_should_stop`` 四分支 + ``init_spec`` 降级路径（M8）
- 集成：真跑子图（mock LLM），2 个 subsystem → ``spec_results`` 有 2 个 key，
  SpecResult 字段完整（验证 ``_dict_merge`` reducer 合并生效，B1 阻断项）
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
    """构造一个 mock 过 complete_with_tools / complete_structured / complete 的 LLMClient。

    generate_initial_spec 走 ctx.backend.execute_node(tools=...) → llm.complete_with_tools
    critique_spec       走 ctx.backend.execute_node(structured_schema=...) → llm.complete_structured
    refine_spec         走 ctx.backend.execute_node() → llm.complete
    SpecContext.__post_init__ 自动包 LangchainBackend，所以 ctx_backend 可以不传。
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
    """构造子图输入 state（SpecContext 展开为 ctx_* 字段）。"""
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
    """构造一个最小 ctx_* 字段集合，用于 _should_stop / init_spec 单元测试。"""
    settings = _settings(tmp_path, **overrides)
    llm = LLMClient(settings)
    return settings, {
        "ctx_llm": llm,
        "ctx_settings": settings,
        "ctx_state": {"workflow_id": "w1", "sources": [], "understanding": "理解"},
    }


# ----------------------------- 单元：init_spec 降级 ----------------------------- #


def test_init_spec_disabled_llm_uses_template(tmp_path):
    """LLM 未启用 → 模板降级，best/current 都填模板，truncated=False。"""
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
    """spec_refine_enabled=False → 同样走模板降级路径。"""
    settings, ctx_fields = _ctx_settings(tmp_path, spec_refine_enabled=False)
    state: SpecSubgraphState = {"sub": _sub(), **ctx_fields}  # type: ignore[typeddict-item]
    result = init_spec(state)
    assert "系统需求" in result["current_spec"]
    assert result["truncated"] is False


# ----------------------------- 单元：_should_stop 四分支 ----------------------------- #


def test_should_stop_pass_verdict_goes_finalize(tmp_path):
    """① verdict==PASS → finalize。"""
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
    """① score>=pass_score（10）→ finalize（即便 verdict 不是 PASS）。"""
    settings, ctx_fields = _ctx_settings(tmp_path)
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "iteration": 1,
        "attempts": [SpecAttempt(version=1, score=10, verdict=SpecVerdict.NEEDS_IMPROVEMENT)],
        "best_score": 10,
        "used_chars": 100,
        "best_improved": True,
    }
    assert _should_stop(state) == "finalize"


def test_should_stop_max_iter(tmp_path):
    """② iteration>=max_iter → finalize。"""
    settings, ctx_fields = _ctx_settings(tmp_path)
    # max_iter 默认 3；构造一个未达标但已达上限的场景
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
        "best_improved": True,  # 本次仍提升，但已到 max_iter
    }
    assert _should_stop(state) == "finalize"


def test_should_stop_score_degradation(tmp_path):
    """③ 本次 score 未超越 best（持平或下降）→ finalize。"""
    settings, ctx_fields = _ctx_settings(tmp_path)
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "iteration": 2,
        "attempts": [
            SpecAttempt(version=1, score=8, verdict=SpecVerdict.NEEDS_IMPROVEMENT),
            SpecAttempt(version=2, score=5, verdict=SpecVerdict.NEEDS_IMPROVEMENT),
        ],
        "best_score": 8,  # 本次 5 < 历史 8 → best_improved=False
        "used_chars": 100,
        "best_improved": False,
    }
    assert _should_stop(state) == "finalize"


def test_should_stop_score_plateau_counts_as_degradation(tmp_path):
    """③ 持平也算退化（与 loop.py 的 ``score <= best_score`` 一致）。"""
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
        "best_improved": False,  # 持平 → 不更新 best → 退化
    }
    assert _should_stop(state) == "finalize"


def test_should_stop_budget_exceeded(tmp_path):
    """④ used_chars > budget*4 → finalize。"""
    settings, ctx_fields = _ctx_settings(tmp_path, spec_refine_budget_tokens=1)
    # budget_tokens=1 → budget_chars=4，used_chars=5000 远超
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
    """四重都不满足 → refine。"""
    settings, ctx_fields = _ctx_settings(tmp_path)
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "iteration": 1,  # < max_iter=3
        "attempts": [SpecAttempt(version=1, score=5, verdict=SpecVerdict.NEEDS_IMPROVEMENT)],
        "best_score": 5,  # 本次 5 > 历史 -1 → 提升
        "used_chars": 100,  # 远低于 budget
        "best_improved": True,
    }
    assert _should_stop(state) == "refine"


def test_should_stop_truncated_short_circuits(tmp_path):
    """工具截断 → 直接 finalize，不再 critique。"""
    _, ctx_fields = _ctx_settings(tmp_path)
    state: SpecSubgraphState = {
        **ctx_fields,  # type: ignore[typeddict-item]
        "truncated": True,
        "iteration": 0,
        "attempts": [],
    }
    assert _should_stop(state) == "finalize"


def test_should_stop_critic_failed(tmp_path):
    """critic 不可用 → finalize（接受当前 spec）。"""
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


# ----------------------------- 集成：真跑子图 ----------------------------- #


def test_subgraph_pass_on_first_critique(tmp_path):
    """首轮 PASS → spec_results 有 1 个 key，attempts 长度=1。"""
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
    """critique→refine→critique 循环到 PASS。"""
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
    """持续未达标 → 到 max_iter 截断，truncated=True。"""
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
    assert sr.final == "V2"  # 最高分版本


def test_subgraph_degradation_keeps_best(tmp_path):
    """③ 退化 → 保留 best（第一次的版本）。"""
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
    assert sr.final == "V0"  # 回退 best（score 8 那版）


def test_subgraph_budget_truncation(tmp_path):
    """④ 极小预算 → 首次 critique 后即触发预算截断。"""
    state = _make_state(
        tmp_path,
        cwt_texts=["V0"],
        critiques=[_crit("NEEDS_IMPROVEMENT", 5, "x" * 5000)],
        refine_texts=["V1"],
        spec_refine_max_iter=3,
        spec_refine_budget_tokens=1,  # 4 字符预算
    )
    graph = build_spec_subgraph()
    result = graph.invoke(state)
    sr: SpecResult = result["spec_results"]["billing-service"]
    assert sr.truncated is True
    assert sr.iterations == 1


def test_subgraph_critic_none_accepts_current(tmp_path):
    """critique_spec 返回 None → 接受当前 spec。"""
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
    """LLM 未启用 → 整个子图走降级模板，used_llm=False。"""
    state = _make_state(tmp_path, agent_api_key=None)
    graph = build_spec_subgraph()
    result = graph.invoke(state)
    sr: SpecResult = result["spec_results"]["billing-service"]
    assert sr.used_llm is False
    assert "系统需求" in sr.final
    assert sr.attempts == []
    assert sr.iterations == 0


def test_subgraph_generate_empty_falls_back_to_template(tmp_path):
    """generate_initial_spec 返回空 → 模板兜底，critic 仍跑。"""
    state = _make_state(
        tmp_path,
        cwt_texts=[""],  # 生成空
        critiques=[_crit("PASS", 10)],
    )
    graph = build_spec_subgraph()
    result = graph.invoke(state)
    sr: SpecResult = result["spec_results"]["billing-service"]
    assert sr.used_llm is True
    assert "系统需求" in sr.final  # 模板兜底


def test_subgraph_tool_truncation_skips_critique(tmp_path):
    """工具截断 → 跳过 critique/refine，直接 finalize。"""
    settings = _settings(tmp_path)
    llm = LLMClient(settings)
    # generate_initial_spec 走 complete_with_tools，截断标记从 ToolLoopResult 来
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
    assert sr.attempts == []          # critique 被跳过
    assert sr.iterations == 0
    assert sr.truncated is False       # tool 截断不触发 refine truncated
    assert sr.used_llm is True
    assert sr.tool_meta["truncated"] is True
    assert sr.tool_meta["reason"] == "tool_loop_truncated"
    assert "规格生成未能完成" in sr.final  # steps.py 重新格式化截断提示


# ----------------------------- 集成：多子系统 reducer 合并（M8 关键）----------------------------- #


def test_subgraph_reducer_merges_two_subsystems(tmp_path):
    """两个子系统各自跑子图 → _dict_merge 合并 spec_results，两个 key 都在。

    这是审计 B1 的回归测试：如果 SpecSubgraphState 未声明
    ``spec_results: Annotated[dict, _dict_merge]``，父图合并时后写会覆盖先写，
    只剩一个 key。本测试直接对子图输出做 reducer 合并，验证数据结构正确。
    """
    graph = build_spec_subgraph()

    # 子系统 1：billing-service，首轮 PASS
    state_a = _make_state(
        tmp_path,
        sub=_sub("billing-service"),
        cwt_texts=["BILLING SPEC"],
        critiques=[_crit("PASS", 11)],
    )
    # 子系统 2：payment-service，refine 一次后 PASS
    state_b = _make_state(
        tmp_path,
        sub=_sub("payment-service"),
        cwt_texts=["PAY V0"],
        critiques=[_crit("NEEDS_IMPROVEMENT", 6, "add idempotency"), _crit("PASS", 10)],
        refine_texts=["PAY V1"],
    )

    result_a = graph.invoke(state_a)
    result_b = graph.invoke(state_b)

    # 模拟父图 Send fan-out 的 reducer 合并
    merged_spec_results = _dict_merge(
        result_a.get("spec_results", {}),
        result_b.get("spec_results", {}),
    )

    # 两个 key 都在（B1 阻断项的核心断言）
    assert set(merged_spec_results) == {"billing-service", "payment-service"}

    # 字段完整：SpecResult 实例 + 关键字段
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
    assert payment.tool_meta  # 非 tool 截断也有 meta（turns/tool_calls 等）


def test_subgraph_reducer_preserves_keys_across_many_subsystems(tmp_path):
    """5 个子系统 → reducer 后 5 个 key 都在（覆盖更接近真实 fan-out 的规模）。"""
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
