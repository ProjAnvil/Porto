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
    """One turn of a multi-turn golden: its own question / reference answer / gold documents."""

    question: str
    reference_answer: str
    gold_doc_ids: list[str] = field(default_factory=list)


@dataclass
class RagGolden:
    question: str
    reference_answer: str
    gold_doc_ids: list[str]
    category: str = "default"
    # Refusal dimension: answer = should answer from the context; refuse = the knowledge base
    # has no answer and an explicit refusal is expected
    expected_behavior: str = "answer"
    # Multi-turn dimension: when not None, turns is the full sequence of turns (first turn
    # included) and question/reference_answer mirror the last turn (so single-turn consumers
    # keep working); always None for single-turn goldens
    turns: list[GoldenTurn] | None = None
    # Extra information such as source / difficulty / how it was built (human|synthetic); goes
    # into the report, not into the metrics
    metadata: dict = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────── #
# EvalProfile: a combination of retrieval settings, mapped onto a slice of the Porto
# Settings. profiles.py defines the named presets; experiment.py builds the KB → runs RAG →
# writes the report for a profile.
# ────────────────────────────────────────────────────────────────────────── #


@dataclass
class EvalProfile:
    """One combination of retrieval settings: a pluggable slice of the production Porto
    Settings.

    Each field maps directly to the parameter of the same name on ChromaVectorStore/Settings,
    and the runner builds an isolated eval KB from it. Switching profiles compares the RAG
    quality of retrieval strategies without any code change.
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
    rerank_model: str = ""  # empty → falls back to agent_model
    # --- query transform ---
    strategy: QueryTransformStrategy = QueryTransformStrategy.NONE
    # Multi-turn coreference resolution: before retrieval an LLM rewrites the current question
    # into a self-contained query using the conversation history (condense_question); can be
    # combined with any strategy
    condense_history: bool = False
    # --- vector backend (fidelity of the retrieval track: chroma | milvus, see
    # provision.build_eval_kb) ---
    vector_backend: str = "chroma"
    # --- eval scope ---
    max_cases: int = 20
