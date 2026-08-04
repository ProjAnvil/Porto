from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

import ollama

from .logging_utils import get_component_logger
from .models.enums import EmbeddingProvider
from .settings import Settings

TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def tokens(text: str) -> list[str]:
    result: list[str] = []
    for raw in TOKEN_RE.findall(text):
        token = raw.lower()
        result.append(token)
        cjk_chars = [ch for ch in token if "\u4e00" <= ch <= "\u9fff"]
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


class EmbeddingClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("embeddings", settings)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.logger.info(
            "embed documents provider=%s model=%s count=%s",
            self.settings.embedding_provider,
            self.settings.embedding_model,
            len(texts),
        )
        if self.settings.embedding_provider == EmbeddingProvider.LOCAL:
            return [local_embed_text(text, self.settings.embedding_dimensions) for text in texts]
        if self.settings.embedding_provider == EmbeddingProvider.OLLAMA:
            client = ollama.Client(host=self.settings.embedding_base_url)
            try:
                response = client.embed(model=self.settings.embedding_model, input=list(texts))
            except Exception:
                self.logger.exception(
                    "ollama embedding failed base_url=%s model=%s count=%s",
                    self.settings.embedding_base_url,
                    self.settings.embedding_model,
                    len(texts),
                )
                raise
            return [list(vector) for vector in response["embeddings"]]
        raise ValueError(f"Unsupported embedding provider: {self.settings.embedding_provider}")

    def embed_query(self, text: str) -> list[float]:
        self.logger.info("embed query provider=%s chars=%s", self.settings.embedding_provider, len(text))
        return self.embed_documents([text])[0]
