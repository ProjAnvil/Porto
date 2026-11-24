from __future__ import annotations

from porto_chatbot.llm import LLMClient
from porto_chatbot.settings import Settings


def test_langchain_prefixed_agent_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("LANGCHAIN_MODEL", "claude-3-5-sonnet-latest")
    monkeypatch.setenv("LANGCHAIN_BASE_URL", "https://example.test")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "test-key")

    settings = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )

    assert settings.agent_provider == "anthropic"
    assert settings.agent_model == "claude-3-5-sonnet-latest"
    assert settings.agent_base_url == "https://example.test"
    assert settings.agent_api_key == "test-key"
    assert settings.agent_temperature == 0.2
    assert settings.agent_max_tokens == 8000


def test_llm_disabled_without_langchain_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "ignored")

    settings = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    client = LLMClient(settings)

    assert settings.agent_api_key in (None, "")
    assert client.enabled is False


def test_conftest_isolates_production_env_file():
    """F5: disable the Settings env_file in test mode to isolate the production ``backend/.env``
    (which holds the real LANGCHAIN_API_KEY).

    pydantic-settings reads the ``env_file`` directly, and conftest's ``_isolate_llm_env``
    only calls ``delenv(os.environ)``, which cannot block that file read; so conftest patches
    ``model_config["env_file"]`` to None right after importing Settings — test-mode Settings
    then reads only from environ (managed by ``_load_env_test`` / ``_isolate_llm_env``) plus
    defaults. Deterministic: it does not depend on whether ``.env`` currently holds a key.
    """
    assert Settings.model_config.get("env_file") is None
