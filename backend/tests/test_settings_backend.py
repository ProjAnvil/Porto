"""Test that backend fields exist with correct defaults and types."""
from porto_chatbot.models.payload import AgentSettingsPayload
from porto_chatbot.settings import Settings


def test_settings_default_backends_are_langchain():
    s = Settings()
    assert s.chatbot_backend == "langchain"
    assert s.workflow_backend == "langchain"


def test_settings_backends_accept_agent_sdk():
    s = Settings()
    s.chatbot_backend = "agent_sdk"
    s.workflow_backend = "agent_sdk"
    assert s.chatbot_backend == "agent_sdk"
    assert s.workflow_backend == "agent_sdk"


def test_payload_accepts_backend_fields():
    payload = AgentSettingsPayload(chatbot_backend="agent_sdk", workflow_backend="langchain")
    assert payload.chatbot_backend == "agent_sdk"
    assert payload.workflow_backend == "langchain"


def test_payload_backends_default_none():
    payload = AgentSettingsPayload()
    assert payload.chatbot_backend is None
    assert payload.workflow_backend is None
