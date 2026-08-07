from __future__ import annotations

from porto_chatbot import main
from porto_chatbot.config_store import ConfigStore
from porto_chatbot.models import (
    AgentSettingsPayload,
    AppSettingsPayload,
    DocumentSettingsPayload,
    RagChatSettingsPayload,
    RagSettingsPayload,
    RagWorkflowSettingsPayload,
)
from porto_chatbot.settings import Settings


def test_config_store_persists_rag_and_agent_settings(tmp_path):
    settings = Settings(
        kb_dirs=[tmp_path / "kb"],
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
            chatbot_backend="agent_sdk",
            workflow_backend="langchain",
            agent_provider="anthropic",
            agent_model="claude-3-5-sonnet-latest",
            agent_base_url="https://example.test",
            agent_api_key="test-key",
            agent_temperature=0.4,
            agent_max_tokens=4096,
        )
    )
    store.save_document_settings(
        DocumentSettingsPayload(
            parse_mode="local",
            local_parser="docling",
            max_tokens=24000,
            max_upload_mb=40,
            max_pdf_pages=320,
        )
    )

    reloaded = ConfigStore(settings)

    assert reloaded.get_rag_settings().embedding_model == "qwen3-embedding:0.6b"
    assert reloaded.get_rag_settings().top_k == 8
    assert reloaded.get_agent_settings().agent_provider == "anthropic"
    assert reloaded.get_agent_settings().agent_temperature == 0.4
    assert reloaded.get_agent_settings().agent_max_tokens == 4096
    # 引擎选择需持久化并在重载后读回（GET /api/settings 依赖此往返）
    assert reloaded.get_agent_settings().chatbot_backend == "agent_sdk"
    assert reloaded.get_agent_settings().workflow_backend == "langchain"
    document = reloaded.get_document_settings()
    assert document.parse_mode == "local"
    assert document.local_parser == "docling"
    assert document.max_tokens == 24000


def test_effective_settings_default_to_qwen_and_agent_params(monkeypatch, tmp_path):
    settings = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / ".porto",
        log_dir=tmp_path / "logs",
        embedding_model="qwen3-embedding:0.6b",
    )
    monkeypatch.setattr(main, "settings", settings)

    app_settings = main.get_app_settings()

    assert app_settings.rag.embedding_model == "qwen3-embedding:0.6b"
    assert app_settings.agent.agent_temperature == 0.2
    assert app_settings.agent.agent_max_tokens == 8000
    assert app_settings.document.parse_mode == "hybrid"
    assert app_settings.document.local_parser == "pypdf"


def test_agent_backend_settings_round_trip_via_api(monkeypatch, tmp_path):
    """引擎选择经 PUT /api/settings 保存后，PUT 响应与独立 GET 都必须回显该字段。

    回归：前端 saveAgentConfig 执行 setAgentConfig(saved.agent)，若响应缺失
    chatbot_backend/workflow_backend，卡片会立即回退为 langchain。
    """
    settings = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / ".porto",
        log_dir=tmp_path / "logs",
    )
    monkeypatch.setattr(main, "settings", settings)

    from porto_chatbot.api.routes.settings import save_app_settings

    saved = save_app_settings(
        AppSettingsPayload(
            agent=AgentSettingsPayload(
                chatbot_backend="agent_sdk",
                workflow_backend="agent_sdk",
                agent_provider="anthropic",
                agent_model="claude-sonnet-5",
            )
        )
    )
    assert saved.agent.chatbot_backend == "agent_sdk"
    assert saved.agent.workflow_backend == "agent_sdk"

    fetched = main.get_app_settings()
    assert fetched.agent.chatbot_backend == "agent_sdk"
    assert fetched.agent.workflow_backend == "agent_sdk"


def test_document_settings_api_values_flow_into_runtime(monkeypatch, tmp_path):
    from porto_chatbot.api.deps import apply_rag_settings
    from porto_chatbot.api.routes.settings import save_app_settings

    settings = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / ".porto",
        log_dir=tmp_path / "logs",
    )
    monkeypatch.setattr(main, "settings", settings)

    saved = save_app_settings(
        AppSettingsPayload(
            document=DocumentSettingsPayload(
                parse_mode="local",
                local_parser="docling",
                max_tokens=22000,
                max_upload_mb=35,
                max_pdf_pages=280,
            )
        )
    )

    assert saved.document.local_parser == "docling"
    runtime = apply_rag_settings()
    assert runtime.document_parse_mode == "local"
    assert runtime.document_local_parser == "docling"
    assert runtime.document_max_tokens == 22000
    assert runtime.document_max_upload_mb == 35
    assert runtime.document_max_pdf_pages == 280


def test_rag_chat_settings_roundtrip(tmp_path):
    from porto_chatbot.config_store import ConfigStore
    from porto_chatbot.settings import Settings

    store = ConfigStore(
        Settings(data_dir=tmp_path / "d", log_dir=tmp_path / "l", kb_dirs=[tmp_path / "kb"])
    )
    assert store.get_rag_chat_settings().intent_routing_mode is None  # 空库
    saved = store.save_rag_chat_settings(
        RagChatSettingsPayload(
            intent_routing_mode="adaptive", query_transform_strategy="hyde"
        )
    )
    assert saved.intent_routing_mode == "adaptive"
    assert saved.query_transform_strategy == "hyde"
    # 重新读
    assert store.get_rag_chat_settings().intent_routing_mode == "adaptive"


def test_rag_workflow_settings_roundtrip(tmp_path):
    from porto_chatbot.config_store import ConfigStore
    from porto_chatbot.settings import Settings

    store = ConfigStore(
        Settings(data_dir=tmp_path / "d", log_dir=tmp_path / "l", kb_dirs=[tmp_path / "kb"])
    )
    saved = store.save_rag_workflow_settings(
        RagWorkflowSettingsPayload(
            query_transform_strategy="multi_query", multi_query_count=5
        )
    )
    assert saved.query_transform_strategy == "multi_query"
    assert store.get_rag_workflow_settings().multi_query_count == 5
