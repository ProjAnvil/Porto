from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from porto_chatbot.health import HealthMonitor
from porto_chatbot.models import DependencyName, DependencyStatus
from porto_chatbot.settings import Settings


def _make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_probe_openai_compatible_ok(tmp_path):
    settings = _make_settings(
        tmp_path,
        embedding_provider="openai_compatible",
        embedding_model="text-embedding-3-small",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key="sk-test",
    )
    monitor = HealthMonitor(
        settings_provider=lambda: settings,
        rag_available=lambda: (True, None),
        rag_status=lambda: None,
    )
    mock_resp = MagicMock()
    mock_resp.data = [MagicMock(embedding=[0.1, 0.2])]
    with patch("openai.OpenAI") as mock_cls:
        mock_cls.return_value.embeddings.create.return_value = mock_resp
        health = monitor._probe_embedding(settings)
    assert health.status == DependencyStatus.OK


def test_probe_openai_compatible_down(tmp_path):
    settings = _make_settings(
        tmp_path,
        embedding_provider="openai_compatible",
        embedding_model="text-embedding-3-small",
        embedding_base_url="https://invalid.example.com/v1",
        embedding_api_key="bad-key",
    )
    monitor = HealthMonitor(
        settings_provider=lambda: settings,
        rag_available=lambda: (True, None),
        rag_status=lambda: None,
    )
    with patch("openai.OpenAI") as mock_cls:
        mock_cls.return_value.embeddings.create.side_effect = Exception("401 Unauthorized")
        health = monitor._probe_embedding(settings)
    assert health.name == DependencyName.EMBEDDING
    assert health.status == DependencyStatus.DOWN


def test_probe_local_ok(tmp_path):
    settings = _make_settings(tmp_path, embedding_provider="local")
    monitor = HealthMonitor(
        settings_provider=lambda: settings,
        rag_available=lambda: (True, None),
        rag_status=lambda: None,
    )
    health = monitor._probe_embedding(settings)
    assert health.status == DependencyStatus.OK
