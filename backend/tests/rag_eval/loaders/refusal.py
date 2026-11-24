"""Refusal (negative rejection) negatives: questions the knowledge base certainly has no
answer for.

Expected behavior: the system under test should state explicitly that "the evidence is
insufficient / this cannot be answered from the context" instead of making up an answer. The
verdict rule lives in ``refusal_detector.py`` (a lexical heuristic, no LLM). The questions
are deliberately picked from general knowledge / technology / literature, which the
DomainRAG corpus (Renmin University admissions) cannot possibly cover.

The corpus reuses DomainRAG (realistic distractors, so retrieval returns irrelevant results
rather than none, which is the real test of refusal ability); when the data is not
downloaded it falls back to the tiny synth set (for pipeline smoke tests).
"""
from __future__ import annotations

from pathlib import Path

from ..schema import CorpusDoc, RagCorpus, RagGolden

# (question, reference note that "a refusal is expected")
_REFUSAL_QA: list[tuple[str, str]] = [
    ("光在真空中的传播速度是多少？", "物理常识，语料库不涉及"),
    ("Python 的 GIL 是什么？", "编程语言知识，语料库不涉及"),
    ("《三体》的作者是谁？", "文学作品，语料库不涉及"),
    ("珠穆朗玛峰的海拔是多少米？", "地理常识，语料库不涉及"),
    ("红烧肉的一般做法是什么？", "菜谱，语料库不涉及"),
    ("2024 年奥运会男子 100 米冠军是谁？", "体育赛事，语料库不涉及"),
    ("HTTP 502 状态码的含义是什么？", "网络协议知识，语料库不涉及"),
    ("太阳系中体积最大的行星是哪颗？", "天文常识，语料库不涉及"),
]

# Minimal fallback when the corpus is missing entirely (keeps the KB / index non-empty so
# the pipeline can run)
_FALLBACK_DOC = CorpusDoc(
    id="refusal_fallback",
    text="中国人民大学本科招生官方网站内容：招生政策、专业目录、历年分数线与院系介绍。",
)


def _refusal_goldens() -> list[RagGolden]:
    return [
        RagGolden(
            question=q,
            reference_answer="（应拒答：明确说明知识库中无相关信息）",
            gold_doc_ids=[],
            category="refusal",
            expected_behavior="refuse",
            metadata={"reason": reason, "constructed": "synthetic"},
        )
        for q, reason in _REFUSAL_QA
    ]


def load_refusal(data_dir: Path | None = None) -> tuple[RagCorpus, list[RagGolden]]:
    """Refusal negatives + distractor corpus (DomainRAG → synth → minimal fallback, falling back
    level by level)."""
    corpus: RagCorpus
    source = "fallback"
    try:
        from .domainrag import load_domainrag

        corpus, _ = load_domainrag(data_dir)
        source = "domainrag"
    except Exception:
        try:
            from .synth import load_synth

            corpus, _ = load_synth()
            source = "synth"
        except Exception:
            corpus = RagCorpus(docs=[_FALLBACK_DOC])
    goldens = _refusal_goldens()
    for g in goldens:
        g.metadata["corpus"] = source
    return corpus, goldens
