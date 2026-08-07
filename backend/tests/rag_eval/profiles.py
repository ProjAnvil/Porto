"""命名检索优化 profile 预设——覆盖 Porto 的策略矩阵。

用法::

    from tests.rag_eval.profiles import PROFILES
    profile = PROFILES["rerank"]          # 取预设

CLI::

    python -m tests.rag_eval.experiment rerank           # 跑单个
    python -m tests.rag_eval.experiment --sweep all      # 全扫
    python -m tests.rag_eval.experiment --list            # 列出
"""
from __future__ import annotations

from porto_chatbot.models.enums import (
    EmbeddingProvider,
    QueryTransformStrategy,
    RetrievalMethod,
)

from .schema import EvalProfile

# ── 基线 ────────────────────────────────────────────────────────────────── #

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
    # ── query transform 策略（搭配 rerank 基座）────────────────────────── #
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
}


def get_profile(name: str) -> EvalProfile:
    """取命名 profile；不存在时报错列出可用项。"""
    if name not in PROFILES:
        avail = ", ".join(sorted(PROFILES))
        raise KeyError(f"未知 profile '{name}'，可用: {avail}")
    return PROFILES[name]
