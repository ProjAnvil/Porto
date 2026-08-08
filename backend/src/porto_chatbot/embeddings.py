from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import ollama

from .logging_utils import get_component_logger
from .models.enums import EmbeddingProvider
from .settings import Settings

TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def tokens(text: str) -> list[str]:
    result: list[str] = []
    for raw in TOKEN_RE.findall(text):
        token = raw.lower()
        result.append(token)
        cjk_chars = [ch for ch in token if "一" <= ch <= "鿿"]
        if cjk_chars:
            result.extend(cjk_chars)
            result.extend("".join(cjk_chars[i : i + 2]) for i in range(len(cjk_chars) - 1))
    return result


def local_embed_text(text: str, dimensions: int = 384) -> list[float]:
    vector = [0.0] * dimensions
    for token in tokens(text):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


# ── Strategy Pattern ──

@runtime_checkable
class EmbeddingBackend(Protocol):
    """Embedding backend strategy interface."""

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a list of texts into vectors."""
        ...


class LocalEmbeddingBackend:
    """Local hash-based embedding backend (no external API)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [local_embed_text(text, self.settings.embedding_dimensions) for text in texts]


class OllamaEmbeddingBackend:
    """Ollama embedding backend (local model server)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = ollama.Client(host=settings.embedding_base_url)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        response = self._client.embed(model=self.settings.embedding_model, input=list(texts))
        return [list(vector) for vector in response["embeddings"]]


class OpenAICompatibleEmbeddingBackend:
    """OpenAI-compatible embedding backend (Jina, Azure OpenAI, etc.)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        import openai

        self._client = openai.OpenAI(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
        )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        response = self._client.embeddings.create(
            model=self.settings.embedding_model,
            input=list(texts),
        )
        return [item.embedding for item in response.data]


# ── Registry ──

EMBEDDING_BACKENDS: dict[EmbeddingProvider, type[EmbeddingBackend]] = {
    EmbeddingProvider.LOCAL: LocalEmbeddingBackend,
    EmbeddingProvider.OLLAMA: OllamaEmbeddingBackend,
    EmbeddingProvider.OPENAI_COMPATIBLE: OpenAICompatibleEmbeddingBackend,
}


# ── Facade ──

class EmbeddingClient:
    """Facade for embedding backends with registry-based dispatch."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("embeddings", settings)
        backend_cls = EMBEDDING_BACKENDS[settings.embedding_provider]
        self._backend = backend_cls(settings)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.logger.info(
            "embed documents provider=%s model=%s count=%s",
            self.settings.embedding_provider,
            self.settings.embedding_model,
            len(texts),
        )
        try:
            return self._backend.embed_documents(texts)
        except Exception:
            self.logger.exception(
                "embedding failed provider=%s model=%s base_url=%s count=%s",
                self.settings.embedding_provider,
                self.settings.embedding_model,
                self.settings.embedding_base_url,
                len(texts),
            )
            raise

    def embed_query(self, text: str) -> list[float]:
        self.logger.info("embed query provider=%s chars=%s", self.settings.embedding_provider, len(text))
        return self.embed_documents([text])[0]
