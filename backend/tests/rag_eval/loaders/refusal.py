"""拒答（negative rejection）负样本集——知识库必然无答案的问题。

期望行为：被测系统应明确说明「依据不足/无法从上下文回答」，而不是编造答案。
判定规则在 ``refusal_detector.py``（词面启发式，零 LLM）。题目刻意选自
DomainRAG 语料（人大招生）不可能覆盖的通用常识/技术/文学领域。

corpus 复用 DomainRAG（真实分布的干扰语料，检索会返回不相关结果而非空结果，
这才是对拒答能力的真实考验）；数据未下载时回退 synth 微型集（链路冒烟用）。
"""
from __future__ import annotations

from pathlib import Path

from ..schema import CorpusDoc, RagCorpus, RagGolden

# (问题, 参考「期望拒答」的说明)
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

# 语料全缺时的最小兜底（保证 KB/索引非空，链路可跑）
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
    """拒答负样本 + 干扰语料（DomainRAG → synth → 最小兜底，逐级回退）。"""
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
