from porto_chatbot.settings import Settings
from porto_chatbot.models import AgentSettingsPayload


def test_settings_defaults():
    s = Settings()
    assert s.agent_request_timeout == 120
    assert s.spec_refine_concurrency == 3
    assert not hasattr(s, "spec_refine_parallel")


def test_settings_bounds():
    import pytest
    with pytest.raises(Exception):
        Settings(spec_refine_concurrency=0)
    with pytest.raises(Exception):
        Settings(spec_refine_concurrency=11)


def test_payload_has_new_fields():
    p = AgentSettingsPayload(spec_refine_concurrency=5, agent_request_timeout=60)
    assert p.spec_refine_concurrency == 5
    assert p.agent_request_timeout == 60
