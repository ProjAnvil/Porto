"""Unit tests for the PortoAgent container.

Since Task 9 PortoAgent is a slim pure container (constructor / _build_critic_llm /
_step); graph / run / _persist / _route_after_evaluate and the per-node delegation methods
are gone:
- End-to-end orchestration (run) is covered by the langgraph StateGraph +
  WorkflowExecutor (see test_workflow_executor.py).
- Node-level delegation (retrieve_knowledge / understand_prd / ...) is gone; nodes are
  called directly.

Task 10: the evaluate node is gone (removed from the graph in Task 9, file cleanup in
Task 10); the tests of the former evaluate node (rubric aggregation + the conditional
back-edge decision) were removed with it.

This file covers:
1. critic_llm container behavior (base_url / api_key fallback, separate client, falling
   back to the generator when not configured).
"""

from __future__ import annotations

from porto_chatbot.agent import PortoAgent

# --------------------- critic model base_url / api_key fallback --------------------- #


def test_critic_llm_inherits_generator_base_url_and_key(sample_settings):
    """When critic_base_url / critic_api_key are not configured, the generator's base_url /
    api_key must be reused."""
    sample_settings.agent_api_key = "gen-key"
    sample_settings.agent_base_url = "https://gen-proxy.example.com/v1"
    sample_settings.agent_model = "gen-model"
    sample_settings.critic_provider = "openai"
    sample_settings.critic_model = "critic-model"
    # critic_base_url / critic_api_key deliberately left unset
    agent = PortoAgent(sample_settings)

    assert agent.critic_llm is not agent.llm  # a separate client
    assert agent.critic_llm.settings.agent_base_url == "https://gen-proxy.example.com/v1"  # falls back to the generator
    assert agent.critic_llm.settings.agent_api_key == "gen-key"  # falls back to the generator
    assert agent.critic_llm.settings.agent_model == "critic-model"  # the critic's own
    assert agent.critic_llm.settings.agent_provider == "openai"


def test_critic_llm_uses_own_base_url_when_provided(sample_settings):
    """A configured critic_base_url is used as is and is not overridden by the generator."""
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
    """Without critic_provider, critic_llm is the generator (self.llm)."""
    agent = PortoAgent(sample_settings)
    assert agent.critic_llm is agent.llm
