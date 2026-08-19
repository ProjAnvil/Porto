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
class GoldenTurn:
    """多轮 golden 中的一轮：独立的问题/参考答案/gold 文档。"""

    question: str
    reference_answer: str
    gold_doc_ids: list[str] = field(default_factory=list)


@dataclass
class RagGolden:
    question: str
    reference_answer: str
    gold_doc_ids: list[str]
    category: str = "default"
    # 拒答维度：answer = 应基于上下文作答；refuse = 知识库无答案，期望明确拒答
    expected_behavior: str = "answer"
    # 多轮维度：非 None 时 turns 为完整轮次序列（含首轮），question/reference_answer
    # 镜像最后一轮（兼容单轮消费方）；单轮 golden 恒为 None
    turns: list[GoldenTurn] | None = None
    # 来源/难度/构造方式（human|synthetic）等附加信息，进报告不进指标
    metadata: dict = field(default_factory=dict)


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
    # 多轮指代消解：检索前用 LLM 结合对话历史把当前问题改写为独立完整查询
    # （condense_question），可与任意 strategy 组合
    condense_history: bool = False
    # --- vector backend（检索轨真实性：chroma | milvus，见 provision.build_eval_kb）---
    vector_backend: str = "chroma"
    # --- eval scope ---
    max_cases: int = 20
