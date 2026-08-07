from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..schema import CorpusDoc, RagCorpus, RagGolden

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "domainrag"
# extractive 单轮 QA：clean、带参考答案与 gold doc id，作为门禁主数据集。
DEFAULT_QA_REL = "_repo/BCM/labeled_data/extractive_qa/basic_qa.jsonl"


def _join_answers(answers: Any) -> str:
    """answers 形如 [["汉语","法语","英语"]]（list of list）；取首个变体拼接。"""
    if not answers:
        return ""
    first = answers[0]
    if isinstance(first, list):
        return "、".join(str(a) for a in first)
    return str(first)


def load_domainrag_from_records(
    corpus_recs: list[dict], qa_recs: list[dict]
) -> tuple[RagCorpus, list[RagGolden]]:
    """纯归一化：raw 记录 → schema（确定性核心，被单测覆盖）。

    corpus 记录：{id, title, url, contents}
    qa 记录：    {question, answers: [[...]], positive_reference: [{id,...}]}
    """
    corpus = RagCorpus(
        docs=[
            CorpusDoc(
                id=str(r.get("id", "")),
                text=str(r.get("contents", "")),
                metadata={"title": r.get("title", ""), "url": r.get("url", "")},
            )
            for r in corpus_recs
        ]
    )
    goldens: list[RagGolden] = []
    for q in qa_recs:
        gold_ids = [
            str(pr.get("id"))
            for pr in q.get("positive_reference", [])
            if pr.get("id") is not None
        ]
        goldens.append(
            RagGolden(
                question=str(q.get("question", "")),
                reference_answer=_join_answers(q.get("answers")),
                gold_doc_ids=gold_ids,
                category=str(q.get("category", "extractive")),
            )
        )
    return corpus, goldens


def _load_corpus_records(corpus_root: Path) -> list[dict]:
    """corpus/.../json_output/*.json（纯文本；跳过 html_output 重复）。"""
    recs: list[dict] = []
    for f in sorted(corpus_root.rglob("json_output/*.json")):
        try:
            recs.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return recs


def _load_qa_records(data_dir: Path) -> list[dict]:
    candidates = [data_dir / DEFAULT_QA_REL]
    if not candidates[0].exists():
        candidates = list(data_dir.rglob("basic_qa.jsonl"))
    if not candidates:
        raise FileNotFoundError(f"未找到 basic_qa.jsonl 于 {data_dir}（运行 make eval-dataset）")
    recs: list[dict] = []
    for f in candidates:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def load_domainrag(data_dir: Path | None = None) -> tuple[RagCorpus, list[RagGolden]]:
    """加载 DomainRAG：corpus/ + basic_qa.jsonl → (RagCorpus, [RagGolden])。

    缺失时抛 FileNotFoundError，由 conftest 捕获后 session fixture skip。
    """
    base = data_dir or DATA_DIR
    corpus_root = base / "corpus"
    if not corpus_root.exists():
        raise FileNotFoundError(f"corpus 目录缺失于 {corpus_root}（运行 make eval-dataset）")
    return load_domainrag_from_records(
        _load_corpus_records(corpus_root), _load_qa_records(base)
    )
