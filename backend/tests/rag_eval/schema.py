from __future__ import annotations

from dataclasses import dataclass, field

from porto_chatbot.models.enums import (
    EmbeddingProvider,
    QueryTransformStrategy,
    RetrievalMethod,
)


@dataclass
class CorpusDoc:
    id: str
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class RagCorpus:
    docs: list[CorpusDoc]


@dataclass
class RagGolden:
    question: str
    reference_answer: str
    gold_doc_ids: list[str]
    category: str = "default"


# ────────────────────────────────────────────────────────────────────────── #
# EvalProfile：一组检索优化配置的组合，映射到 Porto Settings 的一个切片。
# profiles.py 定义命名预设；experiment.py 按 profile 构建 KB → 跑 RAG → 出报告。
# ────────────────────────────────────────────────────────────────────────── #


@dataclass
class EvalProfile:
    """一个检索优化配置组合——对应 Porto 生产 Settings 的一个可插拔切片。

    每个字段直接映射到 ChromaVectorStore/Settings 的同名参数，runner 据此构建
    隔离的 eval KB。切换 profile 即可对比不同检索策略的 RAG 质量，无需改代码。
    """

    name: str
    description: str = ""
    # --- chunking ---
    max_chunk_chars: int = 1400
    chunk_overlap: int = 180
    # --- embedding ---
    embedding_provider: EmbeddingProvider = EmbeddingProvider.LOCAL
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_dimensions: int = 128
    # --- retrieval ---
    retrieval_method: RetrievalMethod = RetrievalMethod.HYBRID
    top_k: int = 6
    # --- rerank ---
    rerank_enabled: bool = False
    rerank_top_n: int = 5
    rerank_model: str = ""  # 留空 → 复用 agent_model
    # --- query transform ---
    strategy: QueryTransformStrategy = QueryTransformStrategy.NONE
    # --- eval scope ---
    max_cases: int = 20
