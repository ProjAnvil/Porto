"""PortoAgent 容器单元测试。

Task 9 之后 PortoAgent 瘦身为纯容器(构造/_build_critic_llm/_step),
graph/run/_persist/_route_after_evaluate/各 node 委托方法已删除:
- 端到端编排(run)由 langgraph StateGraph + WorkflowExecutor 覆盖
  (见 test_workflow_executor.py)。
- 节点级委托(retrieve_knowledge/understand_prd/...)已删除,直接调 nodes。

Task 10:evaluate 节点已删(从 graph 移除于 Task 9,文件清理于 Task 10),
原 evaluate 节点的 rubric 聚合 + 条件回边决策测试一并删除。

本文件覆盖:
1. critic_llm 容器行为(base_url/api_key 回退、独立 client、未配置回退 generator)。
"""

from __future__ import annotations

from porto_chatbot.agent import PortoAgent

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
