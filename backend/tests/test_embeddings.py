from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from porto_chatbot.embeddings import (
    EMBEDDING_BACKENDS,
    EmbeddingClient,
    LocalEmbeddingBackend,
    OllamaEmbeddingBackend,
    OpenAICompatibleEmbeddingBackend,
)
from porto_chatbot.models.enums import EmbeddingProvider
from porto_chatbot.settings import Settings


def _make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        embedding_dimensions=128,
        embedding_provider="local",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ── Registry ──

def test_registry_has_all_providers():
    assert set(EMBEDDING_BACKENDS.keys()) == {
        EmbeddingProvider.LOCAL,
        EmbeddingProvider.OLLAMA,
        EmbeddingProvider.OPENAI_COMPATIBLE,
    }


# ── LocalEmbeddingBackend ──

def test_local_backend_embed(tmp_path):
    settings = _make_settings(tmp_path, embedding_provider="local", embedding_dimensions=64)
    backend = LocalEmbeddingBackend(settings)
    vectors = backend.embed_documents(["hello world", "支付风控"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 64
    assert len(vectors[1]) == 64


# ── OllamaEmbeddingBackend ──

def test_ollama_backend_embed(tmp_path):
    settings = _make_settings(
        tmp_path,
        embedding_provider="ollama",
        embedding_model="qwen3-embedding:0.6b",
        embedding_base_url="http://localhost:11434",
    )
    backend = OllamaEmbeddingBackend(settings)
    mock_client = MagicMock()
    mock_client.embed.return_value = {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
    backend._client = mock_client
    vectors = backend.embed_documents(["text1", "text2"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    mock_client.embed.assert_called_once_with(model="qwen3-embedding:0.6b", input=["text1", "text2"])


# ── OpenAICompatibleEmbeddingBackend ──

def test_openai_compatible_backend_embed(tmp_path):
    settings = _make_settings(
        tmp_path,
        embedding_provider="openai_compatible",
        embedding_model="text-embedding-3-small",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key="sk-test",
    )
    mock_resp = MagicMock()
    mock_resp.data = [
        MagicMock(embedding=[0.1, 0.2, 0.3]),
        MagicMock(embedding=[0.4, 0.5, 0.6]),
    ]
    mock_client = MagicMock()
    mock_client.embeddings.create.return_value = mock_resp
    with patch("openai.OpenAI", return_value=mock_client):
        backend = OpenAICompatibleEmbeddingBackend(settings)
    vectors = backend.embed_documents(["hello", "world"])
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    backend._client.embeddings.create.assert_called_once_with(
        model="text-embedding-3-small", input=["hello", "world"]
    )


# ── EmbeddingClient facade dispatch ──

def test_client_dispatches_to_local(tmp_path):
    settings = _make_settings(tmp_path, embedding_provider="local", embedding_dimensions=32)
    client = EmbeddingClient(settings)
    vec = client.embed_query("test text")
    assert len(vec) == 32


def test_client_dispatches_to_openai_compatible(tmp_path):
    settings = _make_settings(
        tmp_path,
        embedding_provider="openai_compatible",
        embedding_model="jina-embeddings-v3",
        embedding_base_url="https://api.jina.ai/v1",
        embedding_api_key="jina-test",
    )
    mock_resp = MagicMock()
    mock_resp.data = [MagicMock(embedding=[0.1, 0.2])]
    mock_client = MagicMock()
    mock_client.embeddings.create.return_value = mock_resp
    with patch("openai.OpenAI", return_value=mock_client):
        client = EmbeddingClient(settings)
    vec = client.embed_query("test")
    assert vec == [0.1, 0.2]
