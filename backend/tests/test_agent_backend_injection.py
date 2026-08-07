"""Test that PortoAgent gets a backend injected and AgentToolContext supports memory."""
from porto_chatbot.agent.agent import PortoAgent
from porto_chatbot.agent.backends import LangchainBackend
from porto_chatbot.settings import Settings
from porto_chatbot.tools.context import AgentToolContext


def test_porto_agent_has_backend():
    s = Settings()
    agent = PortoAgent(s)
    assert hasattr(agent, "backend")
    assert isinstance(agent.backend, LangchainBackend)


def test_agent_tool_context_memory_fields_exist():
    ctx = AgentToolContext(state={})
    assert hasattr(ctx, "memory_store")
    assert ctx.memory_store is None
    assert hasattr(ctx, "facts_store")
    assert ctx.facts_store is None
