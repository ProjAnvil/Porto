from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .logging_utils import get_component_logger
from .models import AgentSettingsPayload, RagSettingsPayload
from .settings import Settings

RAG_SETTING_KEYS = {
    "embedding_provider",
    "embedding_model",
    "embedding_base_url",
    "chunk_size",
    "chunk_overlap",
    "top_k",
}

AGENT_SETTING_KEYS = {
    "agent_provider",
    "agent_model",
    "agent_base_url",
    "agent_api_key",
    "agent_temperature",
    "agent_max_tokens",
}


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
        safe_keys = sorted(key for key in values if key != "agent_api_key")
        redacted_keys = ["agent_api_key"] if "agent_api_key" in values else []
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
