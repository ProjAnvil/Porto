from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models.enums import (
    ChatbotBackend,
    DocumentParseMode,
    EmbeddingProvider,
    LLMProvider,
    LocalParser,
    RetrievalMethod,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="PORTO_CHATBOT_",
        extra="ignore",
    )

    kb_dirs: list[Path] = Field(default_factory=lambda: [Path.home() / ".scv" / "analysis"])
    data_dir: Path = Path.home() / ".porto"
    log_dir: Path = Path.home() / ".porto" / "logs"
    # 捆绑部署：前端静态导出（next build:static）产物目录，若存在则由后端同源托管
    static_dir: Path = BACKEND_DIR / "static"
    embedding_dimensions: int = 384
    embedding_provider: EmbeddingProvider = EmbeddingProvider.LOCAL
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_base_url: str = "http://127.0.0.1:11434"
    vector_backend: str = "chroma"
    vector_collection: str = "porto_kb"
    memory_collection: str = "porto_memory"
    memory_compact_threshold: int = Field(default=20, ge=4)
    memory_recent_keep: int = Field(default=8, ge=1)
    # Session facts (a2 long-term key facts)
    facts_enabled: bool = True
    facts_max_per_category: int = Field(default=20, ge=1, le=100)
    facts_similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    facts_recent_context_turns: int = Field(default=6, ge=1, le=20)
    facts_provider: LLMProvider | None = None
    facts_model: str | None = None
    context_char_budget: int = Field(default=16000, ge=1000)
    max_chunk_chars: int = 1400
    chunk_overlap: int = 180
    top_k: int = 6

    # --- Agent 引擎选择 ---
    chatbot_backend: ChatbotBackend = ChatbotBackend.LANGCHAIN
    workflow_backend: ChatbotBackend = ChatbotBackend.LANGCHAIN

    agent_provider: LLMProvider = Field(
        default=LLMProvider.OPENAI,
        validation_alias="LANGCHAIN_AGENT_PROVIDER",
    )
    agent_api_key: str | None = Field(default=None, validation_alias="LANGCHAIN_API_KEY")
    agent_base_url: str | None = Field(default=None, validation_alias="LANGCHAIN_BASE_URL")
    agent_model: str = Field(
        default="gpt-4.1-mini",
        validation_alias="LANGCHAIN_MODEL",
    )
    agent_temperature: float = Field(default=0.2, validation_alias="LANGCHAIN_TEMPERATURE")
    agent_max_tokens: int = Field(default=8000, validation_alias="LANGCHAIN_MAX_TOKENS")

    # --- Critic（spec loop 评判模型，缺省回退到 agent_*）---
    critic_provider: LLMProvider | None = None
    critic_api_key: str | None = None
    critic_base_url: str | None = None
    critic_model: str | None = None
    critic_temperature: float = 0.1
    critic_max_tokens: int = 1500

    # --- Spec refine loop（Phase 2）---
    spec_refine_enabled: bool = True
    spec_refine_max_iter: int = Field(default=3, ge=0, le=10)
    spec_refine_concurrency: int = Field(default=3, ge=1, le=10)
    spec_refine_pass_score: int = Field(default=10, ge=0, le=12)
    spec_refine_budget_tokens: int = Field(default=40000, ge=1000)

    # --- Agentic workflow 回边（Phase 3）---
    workflow_rework_enabled: bool = True
    workflow_rework_max_passes: int = Field(default=1, ge=0, le=5)

    # --- Native streaming（Phase 5）---
    agent_stream_enabled: bool = True

    # --- 节点内 tool calling（Phase 0/1）---
    agent_max_tool_turns: int = Field(default=10, ge=1, le=20)

    # --- 整步重跑 turn 上调的隐藏天花板(不暴露给前端,仅 rerun ×1.5 时 cap)---
    tool_turn_hard_cap: int = Field(default=40, ge=1)

    # --- 输出 token 截断续写(finish_reason=length 兜底,对齐 Qwen adaptive output escalation)---
    # 单次回复被 agent_max_tokens 硬切时,先升级 max_tokens(×4)重发同一请求;
    # 仍截断则注入「请继续 + 尾部 200 字」续写拼接,本字段为续写最大轮次。
    max_output_recovery_attempts: int = Field(default=2, ge=0, le=5)

    # --- LLM 请求超时（秒），抗单次调用挂死 ---
    agent_request_timeout: int = Field(default=120, ge=10)

    # --- Agent SDK 子进程保护（抗 Claude CLI 挂起）---
    # Claude CLI 子进程无 stdout 输出的超时（秒），防止多轮工具调用后静默挂起
    agent_sdk_idle_timeout: int = Field(default=120, ge=10)
    # 单次 MCP 工具调用超时（秒）
    agent_tool_timeout: int = Field(default=60, ge=5)

    # --- PRD 文件解析 ---
    document_parse_mode: DocumentParseMode = DocumentParseMode.HYBRID
    document_local_parser: LocalParser = LocalParser.PYPDF
    document_max_tokens: int = Field(default=16000, ge=1000, le=128000)
    document_max_upload_mb: int = Field(default=20, ge=1, le=200)
    document_max_pdf_pages: int = Field(default=200, ge=1, le=1000)

    # --- RAG 监督 / 健康探测 ---
    health_probe_interval: int = Field(default=30, ge=5)
    health_probe_timeout: int = Field(default=5, ge=1)

    # --- 检索算法（vector / bm25 / hybrid）---
    retrieval_method: RetrievalMethod = RetrievalMethod.HYBRID
    bm25_top_k: int = Field(default=20, ge=1)
    # hybrid 融合时向量检索的权重（llama-index QueryFusionRetriever RRF），BM25 权重 = 1 - 该值
    hybrid_vector_weight: float = Field(default=0.5, ge=0.0, le=1.0)

    # --- 重排序（llama-index LLMRerank，检索候选的可选二次精排）---
    rerank_enabled: bool = False
    rerank_top_n: int = Field(default=5, ge=1, le=50)
    # 缺省复用 agent_provider / agent_model / agent_api_key / agent_base_url
    rerank_provider: LLMProvider | None = None
    rerank_model: str | None = None
    rerank_choice_batch_size: int = Field(default=5, ge=1, le=20)

    @field_validator("data_dir", "log_dir", "static_dir", mode="after")
    @classmethod
    def expand_path(cls, value: Path) -> Path:
        return value.expanduser()

    @field_validator("kb_dirs", mode="after")
    @classmethod
    def expand_dirs(cls, value: list[Path]) -> list[Path]:
        return [v.expanduser() for v in value]

    @property
    def kb_path(self) -> Path:
        """向后兼容：首个知识库目录。"""
        return self.kb_dirs[0] if self.kb_dirs else Path()

    @property
    def index_path(self) -> Path:
        return self.data_dir / "kb_index.json"

    @property
    def workflows_dir(self) -> Path:
        return self.data_dir / "workflows"

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def memory_db_path(self) -> Path:
        return self.data_dir / "memory.sqlite3"

    @property
    def settings_db_path(self) -> Path:
        return self.data_dir / "settings.sqlite3"


settings = Settings()
