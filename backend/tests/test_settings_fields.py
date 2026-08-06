import pytest
from pydantic import ValidationError

from porto_chatbot.models import AgentSettingsPayload, DocumentSettingsPayload
from porto_chatbot.settings import Settings


def test_settings_defaults():
    s = Settings()
    assert s.agent_request_timeout == 120
    assert s.spec_refine_concurrency == 4
    assert not hasattr(s, "spec_refine_parallel")


def test_settings_bounds():
    with pytest.raises(ValidationError):
        Settings(spec_refine_concurrency=0)
    with pytest.raises(ValidationError):
        Settings(spec_refine_concurrency=11)


def test_payload_has_new_fields():
    p = AgentSettingsPayload(spec_refine_concurrency=5, agent_request_timeout=60)
    assert p.spec_refine_concurrency == 5
    assert p.agent_request_timeout == 60

    document = DocumentSettingsPayload(
        parse_mode="native",
        local_parser="docling",
        max_tokens=32000,
        max_upload_mb=50,
        max_pdf_pages=300,
    )
    assert document.local_parser == "docling"
    assert document.max_upload_mb == 50


def test_rag_optimization_defaults():
    s = Settings()
    assert s.chat_intent_routing_mode == "binary"
    assert s.chat_query_transform_strategy == "none"
    assert s.workflow_query_transform_strategy == "none"
    assert s.multi_query_count == 4
