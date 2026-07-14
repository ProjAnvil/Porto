"""PortoAgent 容器 + evaluate 节点 单元测试。

Task 9 之后 PortoAgent 瘦身为纯容器(构造/_build_critic_llm/_with_step),
graph/run/_persist/_route_after_evaluate/各 node 委托方法已删除:
- 端到端编排(run)由 WorkflowRunner + WorkflowExecutor 覆盖
  (见 test_workflow_runner.py / test_workflow_executor.py)。
- 节点级委托(retrieve_knowledge/understand_prd/...)已删除,直接调 nodes。
- 回边决策(_route_after_evaluate)内联进 evaluate 节点(见下)。

本文件覆盖:
1. evaluate 节点的 rubric 聚合 + 条件回边决策(经 evaluate_node.evaluate 直调)。
2. critic_llm 容器行为(base_url/api_key 回退、独立 client、未配置回退 generator)。
"""

from __future__ import annotations

from porto_chatbot.agent import PortoAgent
from porto_chatbot.agent.nodes import evaluate as evaluate_node
from porto_chatbot.models import SpecAttempt, SpecResult, Subsystem

# ----------------------------- evaluate 节点:rubric 聚合 + 回边决策 ----------------------------- #


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
    result = evaluate_node.evaluate(agent, state)
    assert result["evaluation"]["spec_rubric_avg"] == 9.0
    assert result["evaluation"]["spec_rubric_min"] == 7


def test_evaluate_marks_rework_on_low_rubric(sample_settings):
    agent = PortoAgent(sample_settings)
    state = _eval_state({"a-service": _spec_result(5)})
    result = evaluate_node.evaluate(agent, state)
    assert result["needs_rework"] is True
    assert result["rework_passes"] == 1


def test_evaluate_no_rework_on_high_rubric(sample_settings):
    agent = PortoAgent(sample_settings)
    state = _eval_state({"a-service": _spec_result(11, "PASS")})
    result = evaluate_node.evaluate(agent, state)
    assert result["needs_rework"] is False
    assert result["rework_passes"] == 0


def test_evaluate_respects_max_passes_zero(sample_settings):
    sample_settings.workflow_rework_max_passes = 0
    agent = PortoAgent(sample_settings)
    state = _eval_state({"a-service": _spec_result(5)})
    result = evaluate_node.evaluate(agent, state)
    assert result["needs_rework"] is False  # passes(0) < 0 为假


def test_evaluate_no_rework_when_disabled(sample_settings):
    sample_settings.workflow_rework_enabled = False
    agent = PortoAgent(sample_settings)
    state = _eval_state({"a-service": _spec_result(5)})
    result = evaluate_node.evaluate(agent, state)
    assert result["needs_rework"] is False


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
