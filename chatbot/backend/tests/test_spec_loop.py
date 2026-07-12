from __future__ import annotations

from typing import Any

from porto_chatbot.llm import LLMClient, ToolLoopResult
from porto_chatbot.models import Subsystem
from porto_chatbot.settings import Settings
from porto_chatbot.specs import (
    SPEC_RUBRIC,
    SpecContext,
    generate_spec_with_loop,
    render_template_spec,
)

# ----------------------------- helpers ----------------------------- #


def _settings(tmp_path, **overrides) -> Settings:
    # agent_api_key 有 validation_alias，init 传字段名无效，故构造后用 setattr
    s = Settings(
        kb_path=tmp_path / "kb",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        agent_provider="openai",
        agent_model="m",
    )
    s.agent_api_key = "k"  # 默认 enabled；测试可传 agent_api_key=None 覆盖
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def _sub(name: str = "billing-service") -> Subsystem:
    return Subsystem(name=name, responsibility="账单管理", capabilities=["出账"], data_entities=["Bill"])


def _make_ctx(
    tmp_path,
    *,
    cwt_texts: list[str] | None = None,
    critiques: list[Any] | None = None,
    refine_texts: list[str] | None = None,
    **settings_kw,
) -> SpecContext:
    settings = _settings(tmp_path, **settings_kw)
    llm = LLMClient(settings)

    cwt_iter = iter(cwt_texts or [])
    llm.complete_with_tools = lambda *a, **k: ToolLoopResult(text=next(cwt_iter, ""))

    crit_iter = iter(critiques or [])
    llm.complete_structured = lambda *a, **k: next(crit_iter, None)

    refine_iter = iter(refine_texts or [])
    llm.complete = lambda *a, **k: next(refine_iter, "")

    state = {"workflow_id": "w1", "prd_text": "demo prd", "understanding": "理解", "sources": []}
    return SpecContext(llm=llm, state=state, settings=settings, vector_store=None)


def _crit(verdict: str, score: int, feedback: str = "fix") -> dict:
    return {"verdict": verdict, "score": score, "feedback": feedback, "per_dimension": {}}


# ----------------------------- 降级路径 ----------------------------- #


def test_loop_disabled_uses_template(tmp_path):
    ctx = _make_ctx(tmp_path, cwt_texts=[], critiques=[], refine_texts=[], agent_api_key=None)
    assert ctx.llm.enabled is False
    result = generate_spec_with_loop(ctx, _sub())
    assert result.used_llm is False
    assert "系统需求" in result.final  # 模板内容
    assert "API 需求" in result.final


def test_loop_refine_disabled_uses_template(tmp_path):
    ctx = _make_ctx(tmp_path, spec_refine_enabled=False)
    result = generate_spec_with_loop(ctx, _sub())
    assert result.used_llm is False
    assert "系统需求" in result.final


def test_loop_generate_empty_falls_back_to_template(tmp_path):
    # LLM 生成返回空 → 用模板，再 critic（critic 也不可用 → 接受模板）
    ctx = _make_ctx(tmp_path, cwt_texts=[""], critiques=[], refine_texts=[])
    result = generate_spec_with_loop(ctx, _sub())
    assert result.used_llm is True
    assert "系统需求" in result.final  # 模板兜底


# ----------------------------- 四重终止 ----------------------------- #


def test_loop_pass_on_first_critique(tmp_path):
    ctx = _make_ctx(
        tmp_path,
        cwt_texts=["INITIAL SPEC"],
        critiques=[_crit("PASS", 10)],
    )
    result = generate_spec_with_loop(ctx, _sub())
    assert result.final == "INITIAL SPEC"
    assert result.iterations == 1
    assert result.truncated is False
    assert len(result.attempts) == 1
    assert result.attempts[0].verdict == "PASS"


def test_loop_refines_until_pass(tmp_path):
    ctx = _make_ctx(
        tmp_path,
        cwt_texts=["V0"],
        critiques=[_crit("NEEDS_IMPROVEMENT", 5, "add error codes"), _crit("PASS", 11)],
        refine_texts=["V1"],
    )
    result = generate_spec_with_loop(ctx, _sub())
    assert result.final == "V1"
    assert result.iterations == 2
    assert result.truncated is False
    assert [a.score for a in result.attempts] == [5, 11]


def test_loop_truncated_at_max_iter(tmp_path):
    # 分数持续上升但不达标 → 到 max_iter 截断
    ctx = _make_ctx(
        tmp_path,
        cwt_texts=["V0"],
        critiques=[_crit("NEEDS_IMPROVEMENT", 3), _crit("NEEDS_IMPROVEMENT", 5), _crit("NEEDS_IMPROVEMENT", 7)],
        refine_texts=["V1", "V2"],
    )
    result = generate_spec_with_loop(ctx, _sub(), max_iter=3)
    assert result.truncated is True
    assert result.iterations == 3
    assert result.final == "V2"  # 最高分版本


def test_loop_score_not_increasing_stops_and_keeps_best(tmp_path):
    # 第二次分数下降 → 停，保留 best（第一次的版本）
    ctx = _make_ctx(
        tmp_path,
        cwt_texts=["V0"],
        critiques=[_crit("NEEDS_IMPROVEMENT", 8), _crit("NEEDS_IMPROVEMENT", 5)],
        refine_texts=["V1"],
    )
    result = generate_spec_with_loop(ctx, _sub(), max_iter=3)
    assert result.iterations == 2
    assert result.truncated is False
    assert result.final == "V0"  # 回退到 best（score 8 那版），不是 V1


def test_loop_critic_none_accepts_current(tmp_path):
    ctx = _make_ctx(
        tmp_path,
        cwt_texts=["V0"],
        critiques=[None],  # critic 解析失败
    )
    result = generate_spec_with_loop(ctx, _sub())
    assert result.final == "V0"
    assert result.iterations == 1
    assert "critic 不可用" in result.attempts[0].feedback_digest


def test_loop_records_attempts_with_versions(tmp_path):
    ctx = _make_ctx(
        tmp_path,
        cwt_texts=["V0"],
        critiques=[_crit("NEEDS_IMPROVEMENT", 4), _crit("NEEDS_IMPROVEMENT", 6), _crit("PASS", 10)],
        refine_texts=["V1", "V2"],
    )
    result = generate_spec_with_loop(ctx, _sub(), max_iter=3)
    assert [a.version for a in result.attempts] == [1, 2, 3]
    assert [a.score for a in result.attempts] == [4, 6, 10]


def test_loop_budget_truncation(tmp_path):
    # 极小预算 → 第一次 critique 后即触发预算截断
    ctx = _make_ctx(
        tmp_path,
        cwt_texts=["V0"],
        critiques=[_crit("NEEDS_IMPROVEMENT", 5, "x" * 5000)],
        refine_texts=["V1"],
        spec_refine_budget_tokens=1,  # 4 字符预算，立刻超
    )
    result = generate_spec_with_loop(ctx, _sub(), max_iter=3)
    assert result.truncated is True
    assert result.iterations == 1


# ----------------------------- rubric ----------------------------- #


def test_spec_rubric_has_six_dimensions():
    keys = {r["key"] for r in SPEC_RUBRIC}
    assert keys == {"coverage", "api", "data_model", "dependency", "verifiability", "consistency"}
    for r in SPEC_RUBRIC:
        assert r["name"] and r["desc"]


def test_render_template_spec_contains_required_sections(tmp_path):
    ctx = _make_ctx(tmp_path, spec_refine_enabled=False)
    out = render_template_spec(ctx, _sub("billing-service"))
    for section in ["执行摘要", "业务能力", "API 需求", "数据模型需求", "集成需求", "验收标准"]:
        assert section in out
    assert "billing-service" in out


# ----------------------------- critic 独立模型（Phase 2 P1）----------------------------- #


def test_critique_falls_back_to_generator_when_no_critic(tmp_path):
    ctx = _make_ctx(tmp_path, cwt_texts=["V0"], critiques=[_crit("PASS", 10)])
    assert ctx.critic_llm is None
    result = generate_spec_with_loop(ctx, _sub())
    assert result.used_llm is True  # 用 generator 作 critic 正常完成
    assert result.attempts[0].verdict == "PASS"


def test_critique_uses_independent_critic_llm(tmp_path):
    # 主 llm 的 complete_structured 返回 FAIL（若被错用会看到 FAIL）
    ctx = _make_ctx(tmp_path, cwt_texts=["V0"], critiques=[_crit("FAIL", 0)])
    critic_llm = LLMClient(_settings(tmp_path))
    critic_llm.complete_structured = lambda *a, **k: {
        "verdict": "PASS", "score": 12, "feedback": "from-critic", "per_dimension": {},
    }
    ctx.critic_llm = critic_llm
    result = generate_spec_with_loop(ctx, _sub())
    # critic_llm 给 PASS，loop 首轮即停；attempts 反映 critic 的判定
    assert result.attempts[0].verdict == "PASS"
    assert result.attempts[0].score == 12
