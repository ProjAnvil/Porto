"""Test AgentBackend Protocol, LangchainBackend, and factory dispatch."""
import asyncio
from unittest.mock import MagicMock

from porto_chatbot.agent.backends import AgentBackend, LangchainBackend, NodeExecutionResult
from porto_chatbot.agent.factory import create_backend
from porto_chatbot.llm import LLMClient
from porto_chatbot.llm.types import ToolCall, ToolLoopResult
from porto_chatbot.settings import Settings


def test_node_execution_result_defaults():
    r = NodeExecutionResult()
    assert r.text == ""
    assert r.structured is None
    assert r.tool_calls == []
    assert r.turns == 0
    assert r.truncated is False
    assert r.reason is None


def _make_mock_llm() -> MagicMock:
    """Create a MagicMock(spec=LLMClient) with .settings populated.

    ``settings`` is an instance attribute (set in __init__), so spec-based mock
    doesn't auto-create it. We attach a real Settings() so get_component_logger
    can access log_dir.
    """
    mock = MagicMock(spec=LLMClient)
    mock.settings = Settings()
    mock.enabled = True
    return mock


def test_langchain_backend_execute_node_with_tools():
    """Tools mode: delegates to complete_with_tools, maps result correctly."""
    mock_llm = _make_mock_llm()
    mock_llm.complete_with_tools.return_value = ToolLoopResult(
        text="understanding text",
        tool_calls=[ToolCall(name="get_prd_text", arguments={}, result="prd...")],
        turns=3,
        truncated=False,
    )
    backend = LangchainBackend(mock_llm)
    result = asyncio.run(
        backend.execute_node(system="sys", user="usr", tools=["fake_tools"])
    )
    assert result.text == "understanding text"
    assert len(result.tool_calls) == 1
    assert result.turns == 3
    assert result.truncated is False


def test_langchain_backend_execute_node_structured():
    """Structured mode: delegates to complete_structured, fills .structured."""
    mock_llm = _make_mock_llm()
    mock_llm.complete_structured.return_value = {"subsystems": [{"name": "svc"}]}
    backend = LangchainBackend(mock_llm)
    result = asyncio.run(
        backend.execute_node(
            system="sys", user="usr", tools=None,
            structured_schema={"type": "object"},
        )
    )
    assert result.structured == {"subsystems": [{"name": "svc"}]}


def test_langchain_backend_execute_node_plain():
    """Plain mode: delegates to complete, fills .text only."""
    mock_llm = _make_mock_llm()
    mock_llm.complete.return_value = "refined spec text"
    backend = LangchainBackend(mock_llm)
    result = asyncio.run(backend.execute_node(system="sys", user="usr"))
    assert result.text == "refined spec text"
    assert result.structured is None


def test_factory_returns_langchain_by_default():
    s = Settings()
    backend = create_backend(s, scope="workflow")
    assert isinstance(backend, LangchainBackend)


def test_factory_returns_langchain_for_chatbot_default():
    s = Settings()
    backend = create_backend(s, scope="chatbot")
    assert isinstance(backend, LangchainBackend)


def test_factory_returns_agent_sdk_when_configured():
    """AgentSDKBackend is created when backend='agent_sdk'.
    This test will be enabled after Task 5 creates AgentSDKBackend."""
    s = Settings()
    s.workflow_backend = "agent_sdk"
    # AgentSDKBackend not yet implemented — expect ImportError or skip
    try:
        backend = create_backend(s, scope="workflow")
        assert not isinstance(backend, LangchainBackend)
    except ImportError:
        pass  # Expected until Task 5


def test_langchain_backend_satisfies_protocol():
    """LangchainBackend should satisfy the AgentBackend Protocol."""
    mock_llm = _make_mock_llm()
    backend = LangchainBackend(mock_llm)
    assert isinstance(backend, AgentBackend)
