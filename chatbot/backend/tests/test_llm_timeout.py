from unittest.mock import patch

from porto_chatbot.llm import LLMClient
from porto_chatbot.settings import Settings


def test_openai_client_uses_configured_timeout():
    s = Settings.model_construct(
        agent_api_key="k", agent_provider="openai", agent_request_timeout=77
    )
    with patch("porto_chatbot.llm.client.OpenAI") as mock_openai:
        LLMClient(s)
    _, kwargs = mock_openai.call_args
    assert kwargs["timeout"] == 77


def test_anthropic_client_uses_configured_timeout():
    s = Settings.model_construct(
        agent_api_key="k", agent_provider="anthropic", agent_request_timeout=99
    )
    with patch("porto_chatbot.llm.client.Anthropic") as mock_anthropic:
        LLMClient(s)
    _, kwargs = mock_anthropic.call_args
    assert kwargs["timeout"] == 99
