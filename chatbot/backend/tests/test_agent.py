from __future__ import annotations

from langgraph.graph import END

from porto_chatbot.agent import PortoAgent
from porto_chatbot.llm import ToolLoopResult
from porto_chatbot.models import SpecAttempt, SpecResult, Subsystem
from porto_chatbot.vector_store import LocalVectorStore


def test_porto_agent_generates_subsystems_and_specs(sample_settings, sample_prd):
    store = LocalVectorStore(sample_settings)
    store.build()
    agent = PortoAgent(sample_settings, store)

    result = agent.run(sample_prd, project_name="支付平台")

    names = {s.name for s in result.subsystems}
    assert "payment-service" in names
    assert "risk-service" in names
    assert result.evaluation["passed"] is True
    assert set(result.specs) == names
    assert len(result.steps) == 5


def test_agent_persists_workflow(sample_settings, sample_prd):
    agent = PortoAgent(sample_settings)
    result = agent.run(sample_prd)

    workflow_dir = sample_settings.workflows_dir / result.workflow_id
    assert (workflow_dir / "workflow.json").exists()
    assert (workflow_dir / "step4" / result.subsystems[0].name / "REQUIREMENTS.md").exists()


# ----------------------------- Phase 1：LLM 驱动路径 ----------------------------- #


def _enabled_agent(sample_settings, sample_prd, monkeypatch, *, cwt_text, structured):
    """构造一个 LLM enabled 的 agent，complete_with_tools/complete_structured 被 mock。"""
    sample_settings.agent_api_key = "k"
    sample_settings.spec_refine_enabled = False  # Phase 1 聚焦 understand+identify，spec 走模板降级
    store = LocalVectorStore(sample_settings)
    store.build()
    agent = PortoAgent(sample_settings, store)
    assert agent.llm.enabled is True
    monkeypatch.setattr(agent.llm, "complete_with_tools", lambda *a, **k: ToolLoopResult(text=cwt_text))
    monkeypatch.setattr(agent.llm, "complete_structured", lambda *a, **k: structured)
    return agent


def test_understand_prd_uses_llm_when_enabled(sample_settings, sample_prd, monkeypatch):
    agent = _enabled_agent(sample_settings, sample_prd, monkeypatch,
                           cwt_text="## LLM 生成的业务理解报告", structured={"subsystems": []})
    # LLM 给了空子系统列表 → identify 会降级，但 understand 必须用 LLM 文本
    result = agent.run(sample_prd, project_name="x")
    assert "LLM 生成的业务理解报告" in result.understanding


def test_understand_prd_falls_back_when_llm_empty(sample_settings, sample_prd, monkeypatch):
    sample_settings.agent_api_key = "k"
    sample_settings.spec_refine_enabled = False  # Phase 1 聚焦 understand+identify，spec 走模板降级
    store = LocalVectorStore(sample_settings)
    store.build()
    agent = PortoAgent(sample_settings, store)
    # LLM enabled 但返回空文本 → understand 必须降级
    monkeypatch.setattr(agent.llm, "complete_with_tools", lambda *a, **k: ToolLoopResult(text=""))
    monkeypatch.setattr(agent.llm, "complete_structured", lambda *a, **k: {"subsystems": []})
    result = agent.run(sample_prd, project_name="x")
    assert "业务需求理解" in result.understanding  # fallback 模板内容


def test_identify_subsystems_uses_llm_when_enabled(sample_settings, sample_prd, monkeypatch):
    agent = _enabled_agent(sample_settings, sample_prd, monkeypatch,
                           cwt_text="LLM 理解",
                           structured={"subsystems": [
                               {"name": "billing-service", "responsibility": "账单管理",
                                "capabilities": ["出账", "对账"], "data_entities": ["Bill"]},
                               {"name": "loyalty-service", "responsibility": "积分"},
                           ]})
    result = agent.run(sample_prd, project_name="x")
    names = {s.name for s in result.subsystems}
    assert "billing-service" in names
    assert "loyalty-service" in names
    # billing 不在 DOMAIN_HINTS 域中 → 证明走了 LLM 而非关键词字典
    assert all(not n.startswith("payment") for n in names)


def test_identify_subsystems_falls_back_when_llm_returns_none(sample_settings, sample_prd, monkeypatch):
    sample_settings.agent_api_key = "k"
    sample_settings.spec_refine_enabled = False  # Phase 1 聚焦 understand+identify，spec 走模板降级
    store = LocalVectorStore(sample_settings)
    store.build()
    agent = PortoAgent(sample_settings, store)
    monkeypatch.setattr(agent.llm, "complete_with_tools", lambda *a, **k: ToolLoopResult(text="理解"))
    monkeypatch.setattr(agent.llm, "complete_structured", lambda *a, **k: None)  # LLM 解析失败
    result = agent.run(sample_prd, project_name="x")
    names = {s.name for s in result.subsystems}
    # 降级到 DOMAIN_HINTS，sample_prd 含"支付""风控"
    assert "payment-service" in names
    assert "risk-service" in names


def test_identify_subsystems_normalizes_malformed_llm_output(sample_settings, sample_prd, monkeypatch):
    agent = _enabled_agent(sample_settings, sample_prd, monkeypatch,
                           cwt_text="理解",
                           structured={"subsystems": [
                               {"name": "ok-service", "responsibility": "ok", "type": "weird"},
                               {"responsibility": "缺名字"},      # 缺 name → 丢弃
                               {"name": "   ", "responsibility": "x"},  # 空 name → 丢弃
                               "not-a-dict",                       # 非 dict → 丢弃
                           ]})
    result = agent.run(sample_prd, project_name="x")
    names = {s.name for s in result.subsystems}
    assert "ok-service" in names
    ok = next(s for s in result.subsystems if s.name == "ok-service")
    assert ok.type == "new"  # 非法 type 归一为 new
    assert len(result.subsystems) == 1  # 其余三条都被丢弃


# ----------------------------- Phase 3：evaluate 聚合 + 条件回边 ----------------------------- #


def _eval_state(spec_results: dict | None = None) -> dict:
    return {
        "workflow_id": "w",
        "prd_text": "p",
        "understanding": "业务理解报告" + "X" * 130,
        "subsystems": [Subsystem(name="a-service", responsibility="负责 A", capabilities=["能力1"])],
        "specs": {"a-service": "包含 API 需求 与 数据模型需求 章节的规格"},
        "spec_results": spec_results or {},
        "steps": [],
    }


def _spec_result(score: int, verdict: str = "NEEDS_IMPROVEMENT") -> SpecResult:
    return SpecResult(
        final="spec",
        attempts=[SpecAttempt(version=1, score=score, verdict=verdict)],
        used_llm=True,
    )


def test_evaluate_aggregates_spec_rubric_scores(sample_settings):
    agent = PortoAgent(sample_settings)
    state = _eval_state({"a-service": _spec_result(7), "b-service": _spec_result(11)})
    result = agent.evaluate(state)
    assert result["evaluation"]["spec_rubric_avg"] == 9.0
    assert result["evaluation"]["spec_rubric_min"] == 7


def test_evaluate_marks_rework_on_low_rubric(sample_settings):
    agent = PortoAgent(sample_settings)
    state = _eval_state({"a-service": _spec_result(5)})
    result = agent.evaluate(state)
    assert result["needs_rework"] is True
    assert result["rework_passes"] == 1


def test_evaluate_no_rework_on_high_rubric(sample_settings):
    agent = PortoAgent(sample_settings)
    state = _eval_state({"a-service": _spec_result(11, "PASS")})
    result = agent.evaluate(state)
    assert result["needs_rework"] is False
    assert result["rework_passes"] == 0


def test_evaluate_respects_max_passes_zero(sample_settings):
    sample_settings.workflow_rework_max_passes = 0
    agent = PortoAgent(sample_settings)
    state = _eval_state({"a-service": _spec_result(5)})
    result = agent.evaluate(state)
    assert result["needs_rework"] is False  # passes(0) < 0 为假


def test_evaluate_no_rework_when_disabled(sample_settings):
    sample_settings.workflow_rework_enabled = False
    agent = PortoAgent(sample_settings)
    state = _eval_state({"a-service": _spec_result(5)})
    result = agent.evaluate(state)
    assert result["needs_rework"] is False


def test_route_after_evaluate_picks_target(sample_settings):
    agent = PortoAgent(sample_settings)
    assert agent._route_after_evaluate({"needs_rework": True}) == "identify_subsystems"
    assert agent._route_after_evaluate({"needs_rework": False}) == END


def _rework_mocks(monkeypatch, agent):
    """understand/generate 走 LLM，critique 恒给低分，触发回边。"""
    monkeypatch.setattr(
        agent.llm, "complete_with_tools",
        lambda *a, **k: ToolLoopResult(text="业务理解报告" + "X" * 130),
    )
    monkeypatch.setattr(agent.llm, "complete", lambda *a, **k: "refined spec")

    def fake_struct(system, user, schema, **k):
        if "架构师" in system:  # identify_subsystems
            return {"subsystems": [{"name": "a-service", "responsibility": "负责 A",
                                    "capabilities": ["能力1"], "data_entities": ["EntityA"]}]}
        if "评审" in system:  # critique_spec
            return {"verdict": "NEEDS_IMPROVEMENT", "score": 5, "feedback": "不够完整"}
        return None

    monkeypatch.setattr(agent.llm, "complete_structured", fake_struct)


def test_end_to_end_rework_loops_then_stops(sample_settings, sample_prd, monkeypatch):
    sample_settings.agent_api_key = "k"
    sample_settings.workflow_rework_enabled = True
    sample_settings.workflow_rework_max_passes = 1
    sample_settings.spec_refine_max_iter = 1  # 每子系统只 critique 一次，简化
    store = LocalVectorStore(sample_settings)
    store.build()
    agent = PortoAgent(sample_settings, store)
    _rework_mocks(monkeypatch, agent)

    result = agent.run(sample_prd, project_name="x")
    identify_count = sum(1 for s in result.steps if s.name == "identify_subsystems")
    evaluate_count = sum(1 for s in result.steps if s.name == "evaluate")
    assert identify_count == 2  # 回边重做一次
    assert evaluate_count == 2
    assert result.evaluation.get("spec_rubric_min") == 5


# ----------------------------- critic 模型 base_url/api_key 回退 ----------------------------- #


def test_critic_llm_inherits_generator_base_url_and_key(sample_settings):
    """critic_base_url / critic_api_key 未配时，必须复用 generator 的 base_url / api_key。"""
    sample_settings.agent_api_key = "gen-key"
    sample_settings.agent_base_url = "https://gen-proxy.example.com/v1"
    sample_settings.agent_model = "gen-model"
    sample_settings.critic_provider = "openai"
    sample_settings.critic_model = "critic-model"
    # critic_base_url / critic_api_key 故意不配
    agent = PortoAgent(sample_settings)

    assert agent.critic_llm is not agent.llm  # 独立 client
    assert agent.critic_llm.settings.agent_base_url == "https://gen-proxy.example.com/v1"  # 回退 generator
    assert agent.critic_llm.settings.agent_api_key == "gen-key"  # 回退 generator
    assert agent.critic_llm.settings.agent_model == "critic-model"  # critic 自己的
    assert agent.critic_llm.settings.agent_provider == "openai"


def test_critic_llm_uses_own_base_url_when_provided(sample_settings):
    """配了 critic_base_url 时用它，不被 generator 覆盖。"""
    sample_settings.agent_api_key = "gen-key"
    sample_settings.agent_base_url = "https://gen-proxy.example.com/v1"
    sample_settings.critic_provider = "anthropic"
    sample_settings.critic_base_url = "https://critic-proxy.example.com"
    sample_settings.critic_api_key = "critic-key"
    agent = PortoAgent(sample_settings)

    assert agent.critic_llm.settings.agent_base_url == "https://critic-proxy.example.com"
    assert agent.critic_llm.settings.agent_api_key == "critic-key"
    assert agent.critic_llm.settings.agent_provider == "anthropic"


def test_critic_llm_falls_back_to_generator_when_unconfigured(sample_settings):
    """未配 critic_provider 时，critic_llm 就是 generator（self.llm）。"""
    agent = PortoAgent(sample_settings)
    assert agent.critic_llm is agent.llm
