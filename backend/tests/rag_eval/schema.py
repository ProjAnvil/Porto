from __future__ import annotations

from dataclasses import dataclass, field


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
