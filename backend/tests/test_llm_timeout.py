from porto_chatbot.llm import LLMClient
from porto_chatbot.settings import Settings


def _settings(tmp_path, **over):
    base = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    ).model_copy(
        update=dict(
            agent_api_key="k",
            agent_model="gpt-4.1-mini",
            agent_provider="openai",
        )
    )
    return base.model_copy(update=over)


def test_openai_client_uses_configured_timeout(tmp_path):
    """ChatOpenAI 把 agent_request_timeout 落到 request_timeout 字段。"""
    c = LLMClient(_settings(tmp_path, agent_provider="openai", agent_request_timeout=77))
    # langchain_openai 把 timeout 存为 request_timeout（float）
    actual = getattr(c._client, "request_timeout", None) or getattr(c._client, "timeout", None)
    assert actual == 77


def test_anthropic_client_uses_configured_timeout(tmp_path):
    """ChatAnthropic 把 agent_request_timeout 落到 default_request_timeout 字段。"""
    c = LLMClient(
        _settings(
            tmp_path,
            agent_provider="anthropic",
            agent_model="claude-sonnet-4-5",
            agent_request_timeout=99,
        )
    )
    actual = (
        getattr(c._client, "default_request_timeout", None)
        or getattr(c._client, "request_timeout", None)
        or getattr(c._client, "timeout", None)
    )
    assert actual == 99
