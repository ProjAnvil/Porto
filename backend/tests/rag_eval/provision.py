from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from porto_chatbot.settings import Settings
from porto_chatbot.vector_store import ChromaVectorStore

from .schema import EvalProfile, RagCorpus

_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_filename(doc_id: str) -> str:
    return _SAFE.sub("_", doc_id)


@dataclass
class EvalKb:
    store: ChromaVectorStore
    settings: Settings


def _build_repo(env: dict[str, str]):
    """Build the VectorRepository according to RAG_EVAL_VECTOR_BACKEND (read from the env dict
    and os.environ).

    chroma (default): ChromaVectorRepository, same as the existing behavior.
    milvus: MilvusVectorRepository (RAG_EVAL_MILVUS_URI / RAG_EVAL_MILVUS_TOKEN, local port
    19530 by default). The retrieval track thereby covers the production Milvus adapter path
    instead of Chroma only.
    """
    merged = {**env, **{k: v for k, v in os.environ.items() if k.startswith("RAG_EVAL_")}}
    if merged.get("RAG_EVAL_VECTOR_BACKEND", "chroma") == "milvus":
        from porto_chatbot.vector_stores.milvus import MilvusVectorRepository

        return MilvusVectorRepository(
            uri=merged.get("RAG_EVAL_MILVUS_URI", "http://localhost:19530"),
            token=merged.get("RAG_EVAL_MILVUS_TOKEN") or None,
        )
    return None  # ChromaVectorStore builds its own Chroma repo by default


def build_eval_kb(
    corpus: RagCorpus,
    tmp_dir: Path,
    profile: EvalProfile,
    *,
    env: dict[str, str] | None = None,
) -> EvalKb:
    """Build an isolated eval KB for ``profile``: write the corpus to disk → Settings → build().

    Never touches the user's real KB (data_dir / chroma_dir / kb_dirs / vector_collection are
    isolated). Each profile field maps directly to the Settings parameter of the same name; with
    rerank on, the agent API credentials are injected automatically (read from ``env``). When
    ``profile.vector_backend == "milvus"`` (or ``RAG_EVAL_VECTOR_BACKEND=milvus``) a Milvus repo
    is injected, replacing the production storage path.

    Parameters
    ----------
    profile:
        Retrieval settings combination (chunk / embedding / rerank / strategy / top_k /
        vector_backend).
    env:
        Parsed ``.env.test``; with rerank on it needs ``LANGCHAIN_API_KEY`` and the like.
    """
    env = env or {}
    kb_dir = tmp_dir / "eval_kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    for doc in corpus.docs:
        (kb_dir / f"{_safe_filename(doc.id)}.md").write_text(doc.text, encoding="utf-8")

    settings_kwargs: dict = {
        "kb_dirs": [kb_dir],
        "data_dir": tmp_dir / "eval_data",
        "log_dir": tmp_dir / "eval_logs",
        "embedding_provider": profile.embedding_provider,
        "embedding_dimensions": profile.embedding_dimensions,
        "vector_collection": f"eval_{profile.name}",  # collection isolated per profile
        "retrieval_method": profile.retrieval_method,
        "max_chunk_chars": profile.max_chunk_chars,
        "chunk_overlap": profile.chunk_overlap,
        "top_k": profile.top_k,
        "rerank_enabled": profile.rerank_enabled,
        "condense_enabled": profile.condense_history,
    }
    # embedding model / base_url (needed by ollama and others)
    if profile.embedding_model:
        settings_kwargs["embedding_model"] = profile.embedding_model
    if profile.embedding_base_url:
        settings_kwargs["embedding_base_url"] = profile.embedding_base_url
    # rerank config: when enabled, inject the agent credentials + rerank parameters
    if profile.rerank_enabled:
        settings_kwargs.update({
            "rerank_top_n": profile.rerank_top_n,
            "agent_provider": env.get("LANGCHAIN_AGENT_PROVIDER", "openai"),
            "agent_api_key": env.get("LANGCHAIN_API_KEY", ""),
            "agent_base_url": env.get("LANGCHAIN_BASE_URL") or None,
        })
        rerank_model = profile.rerank_model or env.get("RAG_EVAL_RERANK_MODEL", "")
        if rerank_model:
            settings_kwargs["rerank_model"] = rerank_model

    settings = Settings(**settings_kwargs)
    store = ChromaVectorStore(settings, repo=_build_repo(env))
    store.build(reset=True)
    return EvalKb(store=store, settings=settings)
