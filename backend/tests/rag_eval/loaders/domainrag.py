"""DomainRAG 数据集加载——7 个子集全覆盖 + 按名选择。

子集（``_repo/BCM/labeled_data/`` 下，corpus 共用 ``corpus/``）::

    extractive    extractive_qa/basic_qa.jsonl          单轮抽取式 QA（原有默认）
    noisy         noisy_qa/noisy_qa_ver3.jsonl          噪声鲁棒（gold + 相似噪音页）
    faithful      faithful_qa/faithful_qa.jsonl         忠实度（含 counterfact 陷阱）
    multidoc      multi-doc_qa/multidoc_qa.jsonl        多文档整合
    structured    structured_qa/structured_qa_twopositive.jsonl  表格结构化抽取
    time          time-sensitive_qa/time_sensitive.jsonl 时间敏感（答案随时间变化）
    conversation  conversation_qa/conversation_qa.jsonl 多轮（history_qa + 指代追问）

各子集 schema 差异在 ``_SUBSET_PARSERS`` 内归一化到 RagGolden：
- ``positive_reference``（dict / list）与 ``positive_references``（复数）都产出 gold doc id；
- 引用无 ``id``（structured / conversation）时按 title/url 对 corpus 反查；
- conversation 记录是 pretty-printed 多行 JSON 拼接（非 jsonl），用 raw_decode 解析，
  产出带 ``turns`` 的多轮 golden。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..schema import CorpusDoc, GoldenTurn, RagCorpus, RagGolden

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "domainrag"
_LABELED = "_repo/BCM/labeled_data"

# subset 名 → labeled_data 下的相对路径
SUBSET_FILES: dict[str, str] = {
    "extractive": "extractive_qa/basic_qa.jsonl",
    "noisy": "noisy_qa/noisy_qa_ver3.jsonl",
    "faithful": "faithful_qa/faithful_qa.jsonl",
    "multidoc": "multi-doc_qa/multidoc_qa.jsonl",
    "structured": "structured_qa/structured_qa_twopositive.jsonl",
    "time_sensitive": "time-sensitive_qa/time_sensitive.jsonl",
    "conversation": "conversation_qa/conversation_qa.jsonl",
}

DEFAULT_SUBSET = "extractive"


def available_subsets() -> list[str]:
    return sorted(SUBSET_FILES)


def _join_answers(answers: Any) -> str:
    """answers 形如 [["汉语","法语","英语"]]（list of list）；取首个变体拼接。"""
    if not answers:
        return ""
    first = answers[0]
    if isinstance(first, list):
        return "、".join(str(a) for a in first)
    return str(first)


# ────────────────────────────────────────────────────────────────────────── #
# corpus
# ────────────────────────────────────────────────────────────────────────── #


def _load_corpus_records(corpus_root: Path) -> list[dict]:
    """corpus/.../json_output/*.json（纯文本；跳过 html_output 重复）。"""
    recs: list[dict] = []
    for f in sorted(corpus_root.rglob("json_output/*.json")):
        try:
            recs.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return recs


def build_corpus(corpus_recs: list[dict]) -> tuple[RagCorpus, dict[str, str], dict[str, str]]:
    """corpus 记录 → RagCorpus，附带 title→id / url→id 反查表（无 id 引用用）。"""
    docs: list[CorpusDoc] = []
    by_title: dict[str, str] = {}
    by_url: dict[str, str] = {}
    for r in corpus_recs:
        doc_id = str(r.get("id", ""))
        title = str(r.get("title", ""))
        url = str(r.get("url", ""))
        docs.append(CorpusDoc(id=doc_id, text=str(r.get("contents", "")),
                              metadata={"title": title, "url": url}))
        if title and doc_id:
            by_title[title] = doc_id
        if url and doc_id:
            by_url[url] = doc_id
    return RagCorpus(docs=docs), by_title, by_url


def _ref_ids(refs: Any, by_title: dict[str, str], by_url: dict[str, str]) -> list[str]:
    """引用记录（dict 或 list[dict]）→ gold doc id 列表；无 id 按 title/url 反查。"""
    if refs is None:
        return []
    items = refs if isinstance(refs, list) else [refs]
    out: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        rid = it.get("id")
        if rid is not None:
            out.append(str(rid))
            continue
        title, url = str(it.get("title", "")), str(it.get("url", ""))
        if title in by_title:
            out.append(by_title[title])
        elif url in by_url:
            out.append(by_url[url])
    return out


# ────────────────────────────────────────────────────────────────────────── #
# 各子集 QA 归一化
# ────────────────────────────────────────────────────────────────────────── #


def _parse_basic(q: dict, by_title: dict, by_url: dict) -> RagGolden:
    return RagGolden(
        question=str(q.get("question", "")),
        reference_answer=_join_answers(q.get("answers")),
        gold_doc_ids=_ref_ids(q.get("positive_reference"), by_title, by_url),
        category="extractive",
    )


def _parse_noisy(q: dict, by_title: dict, by_url: dict) -> RagGolden:
    g = _parse_basic(q, by_title, by_url)
    g.category = "noisy"
    g.metadata["has_noisy_reference"] = bool(q.get("noisy_reference"))
    return g


def _parse_faithful(q: dict, by_title: dict, by_url: dict) -> RagGolden:
    g = _parse_basic(q, by_title, by_url)
    g.category = "faithful"
    g.metadata["counterfact_answers"] = _join_answers(q.get("counterfact_answers"))
    g.metadata["commonsense"] = bool(q.get("commonsense"))
    return g


def _parse_multidoc(q: dict, by_title: dict, by_url: dict) -> RagGolden:
    return RagGolden(
        question=str(q.get("question", "")),
        reference_answer=str(q.get("answer", "")),
        gold_doc_ids=_ref_ids(q.get("positive_references"), by_title, by_url),
        category="multidoc",
    )


def _parse_structured(q: dict, by_title: dict, by_url: dict) -> RagGolden:
    g = _parse_basic(q, by_title, by_url)
    g.category = "structured"
    return g


def _parse_time(q: dict, by_title: dict, by_url: dict) -> RagGolden:
    g = _parse_basic(q, by_title, by_url)
    g.category = "time_sensitive"
    g.metadata["answer_date"] = str(q.get("date", ""))
    return g


def _parse_conversation(q: dict, by_title: dict, by_url: dict) -> RagGolden:
    """history_qa（≥0 轮历史）+ 末轮指代追问 → 多轮 RagGolden。"""
    turns: list[GoldenTurn] = []
    for h in q.get("history_qa") or []:
        turns.append(
            GoldenTurn(
                question=str(h.get("question", "")),
                reference_answer=_join_answers(h.get("answers")),
                gold_doc_ids=_ref_ids(h.get("positive_reference"), by_title, by_url),
            )
        )
    final = GoldenTurn(
        question=str(q.get("question", "")),
        reference_answer=_join_answers(q.get("answers")),
        gold_doc_ids=_ref_ids(q.get("positive_reference"), by_title, by_url),
    )
    turns.append(final)
    return RagGolden(
        question=final.question,
        reference_answer=final.reference_answer,
        gold_doc_ids=final.gold_doc_ids,
        category="conversation",
        turns=turns,
        metadata={"n_turns": len(turns)},
    )


_SUBSET_PARSERS = {
    "extractive": _parse_basic,
    "noisy": _parse_noisy,
    "faithful": _parse_faithful,
    "multidoc": _parse_multidoc,
    "structured": _parse_structured,
    "time_sensitive": _parse_time,
    "conversation": _parse_conversation,
}


# ────────────────────────────────────────────────────────────────────────── #
# 读取与入口
# ────────────────────────────────────────────────────────────────────────── #


def _read_qa_records(path: Path, subset: str) -> list[dict]:
    if not path.exists():
        # 兜底：按文件名 rglob（目录结构变动时仍可定位）
        candidates = list(path.parent.parent.rglob(path.name))
        if not candidates:
            raise FileNotFoundError(f"未找到 {path.name}（运行 make eval-dataset）")
        path = candidates[0]
    text = path.read_text(encoding="utf-8")
    # 数据文件是「单行 jsonl + pretty-printed JSON 对象」混合拼接（basic_qa 实测
    # 129 条单行 + 若干多行记录）——统一 raw_decode 逐对象解析，两种格式通吃。
    dec = json.JSONDecoder()
    recs, idx = [], 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \n\t\r":
            idx += 1
        if idx >= len(text):
            break
        obj, idx = dec.raw_decode(text, idx)
        recs.append(obj)
    return recs


def load_domainrag(
    data_dir: Path | None = None,
    subset: str = DEFAULT_SUBSET,
) -> tuple[RagCorpus, list[RagGolden]]:
    """加载 DomainRAG 指定子集：corpus/ + labeled_data/<subset> → (RagCorpus, [RagGolden])。

    缺失时抛 FileNotFoundError，由 conftest 捕获后 session fixture skip。
    """
    if subset not in SUBSET_FILES:
        raise KeyError(f"未知 DomainRAG 子集 '{subset}'，可用: {available_subsets()}")
    base = data_dir or DATA_DIR
    corpus_root = base / "corpus"
    if not corpus_root.exists():
        raise FileNotFoundError(f"corpus 目录缺失于 {corpus_root}（运行 make eval-dataset）")
    qa_path = base / _LABELED / SUBSET_FILES[subset]
    return load_domainrag_from_records(
        _load_corpus_records(corpus_root), _read_qa_records(qa_path, subset), subset=subset
    )


def load_domainrag_from_records(
    corpus_recs: list[dict],
    qa_recs: list[dict],
    subset: str = DEFAULT_SUBSET,
) -> tuple[RagCorpus, list[RagGolden]]:
    """纯归一化：raw 记录 → schema（确定性核心，被单测覆盖）。"""
    corpus, by_title, by_url = build_corpus(corpus_recs)
    parser = _SUBSET_PARSERS.get(subset, _parse_basic)
    goldens = [parser(q, by_title, by_url) for q in qa_recs]
    return corpus, goldens
