from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .logging_utils import get_component_logger
from .models import AgentSettingsPayload, DocumentSettingsPayload, RagSettingsPayload
from .settings import Settings

RAG_SETTING_KEYS = {
    "embedding_provider",
    "embedding_model",
    "embedding_base_url",
    "chunk_size",
    "chunk_overlap",
    "top_k",
    "kb_dirs",
    "retrieval_method",
    "bm25_top_k",
    "hybrid_vector_weight",
    "rerank_enabled",
    "rerank_top_n",
    "rerank_provider",
    "rerank_model",
    "rerank_choice_batch_size",
}

AGENT_SETTING_KEYS = {
    "agent_provider",
    "agent_model",
    "agent_base_url",
    "agent_api_key",
    "agent_temperature",
    "agent_max_tokens",
    "critic_provider",
    "critic_model",
    "critic_base_url",
    "critic_api_key",
    "critic_temperature",
    "critic_max_tokens",
    "spec_refine_enabled",
    "spec_refine_max_iter",
    "spec_refine_concurrency",
    "spec_refine_pass_score",
    "spec_refine_budget_tokens",
    "workflow_rework_enabled",
    "workflow_rework_max_passes",
    "memory_compact_threshold",
    "memory_recent_keep",
    "context_char_budget",
    "agent_stream_enabled",
    "agent_max_tool_turns",
    "agent_request_timeout",
}

DOCUMENT_SETTING_KEYS = {
    "parse_mode",
    "local_parser",
    "max_tokens",
    "max_upload_mb",
    "max_pdf_pages",
}

SENSITIVE_SETTING_KEYS = {"agent_api_key", "critic_api_key"}


class ConfigStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("config_store", settings)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.logger.info("config store ready db=%s", self.settings.settings_db_path)

    def get_rag_settings(self) -> RagSettingsPayload:
        return RagSettingsPayload(**self._get_namespace("rag", RAG_SETTING_KEYS))

    def save_rag_settings(self, payload: RagSettingsPayload) -> RagSettingsPayload:
        current = self.get_rag_settings().model_dump(exclude_none=True)
        current.update(payload.model_dump(exclude_none=True))
        saved = RagSettingsPayload(**current)
        self._save_namespace("rag", saved.model_dump(exclude_none=True))
        return saved

    def get_agent_settings(self) -> AgentSettingsPayload:
        return AgentSettingsPayload(**self._get_namespace("agent", AGENT_SETTING_KEYS))

    def save_agent_settings(self, payload: AgentSettingsPayload) -> AgentSettingsPayload:
        current = self.get_agent_settings().model_dump(exclude_none=True)
        current.update(payload.model_dump(exclude_none=True))
        saved = AgentSettingsPayload(**current)
        self._save_namespace("agent", saved.model_dump(exclude_none=True))
        return saved

    def get_document_settings(self) -> DocumentSettingsPayload:
        return DocumentSettingsPayload(
            **self._get_namespace("document", DOCUMENT_SETTING_KEYS)
        )

    def save_document_settings(
        self, payload: DocumentSettingsPayload
    ) -> DocumentSettingsPayload:
        current = self.get_document_settings().model_dump(exclude_none=True)
        current.update(payload.model_dump(exclude_none=True))
        saved = DocumentSettingsPayload(**current)
        self._save_namespace("document", saved.model_dump(exclude_none=True))
        return saved

    def _get_namespace(self, namespace: str, keys: set[str]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        with sqlite3.connect(self.settings.settings_db_path) as conn:
            rows = conn.execute(
                """
                SELECT key, value
                FROM app_settings
                WHERE namespace = ?
                """,
                (namespace,),
            ).fetchall()
        for key, raw_value in rows:
            if key in keys:
                values[key] = json.loads(raw_value)
        self.logger.info("settings load namespace=%s keys=%s", namespace, sorted(values))
        return values

    def _save_namespace(self, namespace: str, values: dict[str, Any]) -> None:
        updated_at = datetime.now(UTC).isoformat()
        safe_keys = sorted(key for key in values if key not in SENSITIVE_SETTING_KEYS)
        redacted_keys = sorted(k for k in SENSITIVE_SETTING_KEYS if k in values)
        with sqlite3.connect(self.settings.settings_db_path) as conn:
            for key, value in values.items():
                conn.execute(
                    """
                    INSERT INTO app_settings (namespace, key, value, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(namespace, key)
                    DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (namespace, key, json.dumps(value, ensure_ascii=False), updated_at),
                )
        self.logger.info(
            "settings saved namespace=%s keys=%s redacted_keys=%s",
            namespace,
            safe_keys,
            redacted_keys,
        )

    def _init_db(self) -> None:
        with sqlite3.connect(self.settings.settings_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (namespace, key)
                )
                """
            )
