from __future__ import annotations

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
    API 凭证（从 ``env`` 读取）。

    Parameters
    ----------
    profile:
        检索优化配置组合（chunk/embedding/rerank/strategy/top_k）。
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
        if profile.rerank_model:
            settings_kwargs["rerank_model"] = profile.rerank_model

    settings = Settings(**settings_kwargs)
    store = ChromaVectorStore(settings)
    store.build(reset=True)
    return EvalKb(store=store, settings=settings)
