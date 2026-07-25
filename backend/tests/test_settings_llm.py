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
    """F5: 测试态禁用 Settings 的 env_file,隔离生产 ``backend/.env``(含真 LANGCHAIN_API_KEY)。

    pydantic-settings 直读 ``env_file`` 文件,conftest 的 ``_isolate_llm_env`` 只
    ``delenv(os.environ)`` 挡不住文件读取;故 conftest 在 import Settings 后把
    ``model_config["env_file"]`` patch 成 None —— 测试态 Settings 只从 environ(由
    ``_load_env_test`` / ``_isolate_llm_env`` 管控)+ defaults 读。确定性:不依赖
    ``.env`` 当前是否含 key。
    """
    assert Settings.model_config.get("env_file") is None
