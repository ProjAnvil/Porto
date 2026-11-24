"""Named retrieval profile presets covering Porto's strategy matrix.

Usage::

    from tests.rag_eval.profiles import PROFILES
    profile = PROFILES["rerank"]          # pick a preset

CLI::

    python -m tests.rag_eval.experiment rerank           # run one
    python -m tests.rag_eval.experiment --sweep all      # sweep all
    python -m tests.rag_eval.experiment --list            # list profiles
"""
from __future__ import annotations

from porto_chatbot.models.enums import (
    EmbeddingProvider,
    QueryTransformStrategy,
)

from .schema import EvalProfile

# ── Baseline ──────────────────────────────────────────────────────────── #

PROFILES: dict[str, EvalProfile] = {
    "baseline": EvalProfile(
        name="baseline",
        description="基线: local hash embedding, chunk=1400, 无 rerank, NONE 策略",
    ),
    # ── chunking ────────────────────────────────────────────────────────── #
    "smallchunk": EvalProfile(
        name="smallchunk",
        description="小 chunk: chunk=600, 无 rerank（测纯 chunk 粒度影响）",
        max_chunk_chars=600,
    ),
    # ── rerank ──────────────────────────────────────────────────────────── #
    "rerank": EvalProfile(
        name="rerank",
        description="小 chunk + flash rerank top_n=3（P0 改进）",
        max_chunk_chars=600,
        rerank_enabled=True,
        rerank_top_n=3,
        rerank_model="deepseek-v4-flash",
    ),
    # ── embedding ───────────────────────────────────────────────────────── #
    "semantic": EvalProfile(
        name="semantic",
        description="qwen3 语义 embedding + 小 chunk + rerank（P1 改进）",
        max_chunk_chars=600,
        embedding_provider=EmbeddingProvider.OLLAMA,
        embedding_model="qwen3-embedding:0.6b",
        embedding_base_url="http://127.0.0.1:11434",
        embedding_dimensions=1024,
        rerank_enabled=True,
        rerank_top_n=3,
        rerank_model="deepseek-v4-flash",
    ),
    # Isolate one variable: only the semantic embedding changes; chunk / rerank are identical
    # to baseline
    "semantic_embed": EvalProfile(
        name="semantic_embed",
        description="qwen3 语义 embedding（隔离变量：同 baseline chunk/无 rerank）",
        embedding_provider=EmbeddingProvider.OLLAMA,
        embedding_model="qwen3-embedding:0.6b",
        embedding_base_url="http://127.0.0.1:11434",
        embedding_dimensions=1024,
    ),
    # ── Query-transform strategies (on the rerank base) ─────────────────── #
    "hyde": EvalProfile(
        name="hyde",
        description="HyDE 假设文档 + 小 chunk + rerank",
        max_chunk_chars=600,
        rerank_enabled=True,
        rerank_top_n=3,
        rerank_model="deepseek-v4-flash",
        strategy=QueryTransformStrategy.HYDE,
    ),
    "multi_query": EvalProfile(
        name="multi_query",
        description="Multi-Query RRF 融合 + 小 chunk + rerank",
        max_chunk_chars=600,
        rerank_enabled=True,
        rerank_top_n=3,
        rerank_model="deepseek-v4-flash",
        strategy=QueryTransformStrategy.MULTI_QUERY,
    ),
    "decomposition": EvalProfile(
        name="decomposition",
        description="子问题分解 + 小 chunk + rerank",
        max_chunk_chars=600,
        rerank_enabled=True,
        rerank_top_n=3,
        rerank_model="deepseek-v4-flash",
        strategy=QueryTransformStrategy.DECOMPOSITION,
    ),
    "step_back": EvalProfile(
        name="step_back",
        description="Step-back 抽象 + 小 chunk + rerank",
        max_chunk_chars=600,
        rerank_enabled=True,
        rerank_top_n=3,
        rerank_model="deepseek-v4-flash",
        strategy=QueryTransformStrategy.STEP_BACK,
    ),
    # ── Multi-turn coreference resolution (condense) ───────────────────── #
    # condense rewrites the current question into a standalone query from the history,
    # before retrieval. Clean A/B: identical to baseline except condense_history
    "condense": EvalProfile(
        name="condense",
        description="多轮指代消解：baseline 基座 + 历史感知查询改写（condense_question）",
        condense_history=True,
    ),
    "condense_rerank": EvalProfile(
        name="condense_rerank",
        description="指代消解 + 小 chunk + flash rerank（多轮组合拳）",
        max_chunk_chars=600,
        rerank_enabled=True,
        rerank_top_n=3,
        rerank_model="deepseek-v4-flash",
        condense_history=True,
    ),
    # Coreference resolution + semantic embedding: they target the two main multi-turn
    # bottlenecks (missing rewriting / hash embedding being too weak)
    "condense_semantic": EvalProfile(
        name="condense_semantic",
        description="指代消解 + qwen3 语义 embedding（chunk/rerank 同 baseline）",
        embedding_provider=EmbeddingProvider.OLLAMA,
        embedding_model="qwen3-embedding:0.6b",
        embedding_base_url="http://127.0.0.1:11434",
        embedding_dimensions=1024,
        condense_history=True,
    ),
    # ── Single-variable levers (ablation of the four levers) ───────────── #
    # identical to baseline except for the lever itself
    "bigchunk_rerank": EvalProfile(
        name="bigchunk_rerank",
        description="lever: rerank（1400 chunk + flash rerank top5，其余同 baseline）",
        rerank_enabled=True,
        rerank_top_n=5,
        rerank_model="deepseek-v4-flash",
    ),
    "bigchunk_multi_query": EvalProfile(
        name="bigchunk_multi_query",
        description="lever: multi_query（1400 chunk + Multi-Query RRF，无 rerank）",
        strategy=QueryTransformStrategy.MULTI_QUERY,
    ),
}


def get_profile(name: str) -> EvalProfile:
    """Look up a named profile; raise with the available names when it does not exist."""
    if name not in PROFILES:
        avail = ", ".join(sorted(PROFILES))
        raise KeyError(f"未知 profile '{name}'，可用: {avail}")
    return PROFILES[name]
