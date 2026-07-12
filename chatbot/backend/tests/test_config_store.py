from __future__ import annotations

from porto_chatbot import main
from porto_chatbot.config_store import ConfigStore
from porto_chatbot.models import AgentSettingsPayload, RagSettingsPayload
from porto_chatbot.settings import Settings


def test_config_store_persists_rag_and_agent_settings(tmp_path):
    settings = Settings(
        kb_path=tmp_path / "kb",
        data_dir=tmp_path / ".porto",
        log_dir=tmp_path / "logs",
        embedding_model="qwen3-embedding:0.6b",
    )
    store = ConfigStore(settings)

    store.save_rag_settings(
        RagSettingsPayload(
            embedding_provider="ollama",
            embedding_model="qwen3-embedding:0.6b",
            embedding_base_url="http://127.0.0.1:11434",
            chunk_size=1600,
            chunk_overlap=200,
            top_k=8,
        )
    )
    store.save_agent_settings(
        AgentSettingsPayload(
            agent_provider="anthropic",
            agent_model="claude-3-5-sonnet-latest",
            agent_base_url="https://example.test",
            agent_api_key="test-key",
            agent_temperature=0.4,
            agent_max_tokens=4096,
        )
    )

    reloaded = ConfigStore(settings)

    assert reloaded.get_rag_settings().embedding_model == "qwen3-embedding:0.6b"
    assert reloaded.get_rag_settings().top_k == 8
    assert reloaded.get_agent_settings().agent_provider == "anthropic"
    assert reloaded.get_agent_settings().agent_temperature == 0.4
    assert reloaded.get_agent_settings().agent_max_tokens == 4096


def test_effective_settings_default_to_qwen_and_agent_params(monkeypatch, tmp_path):
    settings = Settings(
        kb_path=tmp_path / "kb",
        data_dir=tmp_path / ".porto",
        log_dir=tmp_path / "logs",
        embedding_model="qwen3-embedding:0.6b",
    )
    monkeypatch.setattr(main, "settings", settings)

    app_settings = main.get_app_settings()

    assert app_settings.rag.embedding_model == "qwen3-embedding:0.6b"
    assert app_settings.agent.agent_temperature == 0.2
    assert app_settings.agent.agent_max_tokens == 2000
