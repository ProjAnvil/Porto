from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="PORTO_CHATBOT_",
        extra="ignore",
    )

    kb_path: Path = Path.home() / ".scv" / "analysis"
    data_dir: Path = Path.home() / ".porto"
    log_dir: Path = Path.home() / ".porto" / "logs"
    embedding_dimensions: int = 384
    embedding_provider: Literal["local", "ollama"] = "local"
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_base_url: str = "http://127.0.0.1:11434"
    vector_backend: Literal["chroma"] = "chroma"
    vector_collection: str = "porto_kb"
    memory_collection: str = "porto_memory"
    memory_compact_threshold: int = Field(default=20, ge=4)
    memory_recent_keep: int = Field(default=8, ge=1)
    context_char_budget: int = Field(default=16000, ge=1000)
    max_chunk_chars: int = 1400
    chunk_overlap: int = 180
    top_k: int = 6

    agent_provider: Literal["openai", "anthropic"] = Field(
        default="openai",
        validation_alias="LANGCHAIN_AGENT_PROVIDER",
    )
    agent_api_key: str | None = Field(default=None, validation_alias="LANGCHAIN_API_KEY")
    agent_base_url: str | None = Field(default=None, validation_alias="LANGCHAIN_BASE_URL")
    agent_model: str = Field(
        default="gpt-4.1-mini",
        validation_alias="LANGCHAIN_MODEL",
    )
    agent_temperature: float = Field(default=0.2, validation_alias="LANGCHAIN_TEMPERATURE")
    agent_max_tokens: int = Field(default=2000, validation_alias="LANGCHAIN_MAX_TOKENS")

    # --- Critic（spec loop 评判模型，缺省回退到 agent_*）---
    critic_provider: Literal["openai", "anthropic"] | None = None
    critic_api_key: str | None = None
    critic_base_url: str | None = None
    critic_model: str | None = None
    critic_temperature: float = 0.1
    critic_max_tokens: int = 1500

    # --- Spec refine loop（Phase 2）---
    spec_refine_enabled: bool = True
    spec_refine_max_iter: int = Field(default=3, ge=0, le=10)
    spec_refine_parallel: bool = True
    spec_refine_pass_score: int = Field(default=10, ge=0, le=12)
    spec_refine_budget_tokens: int = Field(default=40000, ge=1000)

    # --- Agentic workflow 回边（Phase 3）---
    workflow_rework_enabled: bool = True
    workflow_rework_max_passes: int = Field(default=1, ge=0, le=5)

    # --- Native streaming（Phase 5）---
    agent_stream_enabled: bool = True

    # --- 节点内 tool calling（Phase 0/1）---
    agent_max_tool_turns: int = Field(default=4, ge=1, le=20)

    @field_validator("kb_path", "data_dir", "log_dir", mode="after")
    @classmethod
    def expand_path(cls, value: Path) -> Path:
        return value.expanduser()

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
