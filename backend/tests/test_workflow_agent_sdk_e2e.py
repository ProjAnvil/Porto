# backend/tests/test_workflow_agent_sdk_e2e.py
"""End-to-end integration test: AgentSDKBackend.execute_node via real Claude API.

Requires the Claude Code CLI to be authenticated (``~/.claude/`` config or
``ANTHROPIC_API_KEY`` env var). Marked ``@pytest.mark.integration`` so CI can
exclude it via ``-m "not integration"`` or ``--ignore``.

Run manually:
    cd backend && uv run pytest tests/test_workflow_agent_sdk_e2e.py -v -m integration
"""
from __future__ import annotations

import asyncio

import pytest

from porto_chatbot.agent.backends import NodeExecutionResult
from porto_chatbot.agent.factory import create_backend
from porto_chatbot.settings import Settings


@pytest.mark.integration
@pytest.mark.skipif(
    not Settings().agent_api_key,
    reason="No agent_api_key configured (set LANGCHAIN_API_KEY in .env.test)",
)
def test_agent_sdk_execute_node_basic():
    """Verify AgentSDKBackend.execute_node returns text for a simple prompt.

    This spawns the real Claude Code CLI subprocess via claude-agent-sdk.
    Skipped unless an API key is available at collection time.
    """
    settings = Settings()
    settings.workflow_backend = "agent_sdk"
    backend = create_backend(settings, scope="workflow")

    result = asyncio.run(
        backend.execute_node(
            system="You are a test assistant. Reply with exactly: hello",
            user="test",
        )
    )

    assert isinstance(result, NodeExecutionResult)
    assert len(result.text) > 0
    assert not result.truncated
