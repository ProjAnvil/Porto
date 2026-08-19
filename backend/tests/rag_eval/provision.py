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
    """按 RAG_EVAL_VECTOR_BACKEND 构造 VectorRepository（env dict 与 os.environ 双来源）。

    chroma（默认）：ChromaVectorRepository——与既有行为一致。
    milvus：MilvusVectorRepository（RAG_EVAL_MILVUS_URI / RAG_EVAL_MILVUS_TOKEN，默认
    本地 19530）。检索轨由此覆盖生产 Milvus 适配路径，而不只测 Chroma。
    """
    merged = {**env, **{k: v for k, v in os.environ.items() if k.startswith("RAG_EVAL_")}}
    if merged.get("RAG_EVAL_VECTOR_BACKEND", "chroma") == "milvus":
        from porto_chatbot.vector_stores.milvus import MilvusVectorRepository

        return MilvusVectorRepository(
            uri=merged.get("RAG_EVAL_MILVUS_URI", "http://localhost:19530"),
            token=merged.get("RAG_EVAL_MILVUS_TOKEN") or None,
        )
    return None  # ChromaVectorStore 默认自建 Chroma repo


def build_eval_kb(
    corpus: RagCorpus,
    tmp_dir: Path,
    profile: EvalProfile,
    *,
    env: dict[str, str] | None = None,
) -> EvalKb:
    """按 ``profile`` 构建 isolation eval KB：corpus 落盘 → Settings → build()。

    全程与用户真实 KB 零交集（data_dir / chroma_dir / kb_dirs / vector_collection 隔离）。
    profile 的每个字段直接映射到 Settings 同名参数；rerank 启用时自动注入 agent
    API 凭证（从 ``env`` 读取）。``profile.vector_backend == "milvus"``（或
    ``RAG_EVAL_VECTOR_BACKEND=milvus``）时注入 Milvus repo，覆盖生产存储路径。

    Parameters
    ----------
    profile:
        检索优化配置组合（chunk/embedding/rerank/strategy/top_k/vector_backend）。
    env:
        ``.env.test`` 解析结果；rerank 启用时需要 ``LANGCHAIN_API_KEY`` 等。
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
        "vector_collection": f"eval_{profile.name}",  # 按 profile 隔离 collection
        "retrieval_method": profile.retrieval_method,
        "max_chunk_chars": profile.max_chunk_chars,
        "chunk_overlap": profile.chunk_overlap,
        "top_k": profile.top_k,
        "rerank_enabled": profile.rerank_enabled,
        "condense_enabled": profile.condense_history,
    }
    # embedding model / base_url（ollama 等需要）
    if profile.embedding_model:
        settings_kwargs["embedding_model"] = profile.embedding_model
    if profile.embedding_base_url:
        settings_kwargs["embedding_base_url"] = profile.embedding_base_url
    # rerank 配置：启用时注入 agent 凭证 + rerank 参数
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
