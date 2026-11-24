"""DomainRAG dataset loading: all 7 subsets + selection by name.

Subsets (under ``_repo/BCM/labeled_data/``; the corpus is shared through ``corpus/``)::

    extractive    extractive_qa/basic_qa.jsonl          single-turn extractive QA (the original default)
    noisy         noisy_qa/noisy_qa_ver3.jsonl          noise robustness (gold + similar noise pages)
    faithful      faithful_qa/faithful_qa.jsonl         faithfulness (with counterfact traps)
    multidoc      multi-doc_qa/multidoc_qa.jsonl        multi-document synthesis
    structured    structured_qa/structured_qa_twopositive.jsonl  structured table extraction
    time          time-sensitive_qa/time_sensitive.jsonl time-sensitive (the answer changes over time)
    conversation  conversation_qa/conversation_qa.jsonl multi-turn (history_qa + coreference follow-ups)

Schema differences between subsets are normalized to RagGolden inside
``_SUBSET_PARSERS``:
- ``positive_reference`` (dict / list) and ``positive_references`` (plural) both yield gold
doc ids;
- references without an ``id`` (structured / conversation) are looked up in the corpus by
  title / url;
- conversation records are pretty-printed multi-line JSON objects concatenated together
  (not jsonl) and parsed with raw_decode, yielding multi-turn goldens with ``turns``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..schema import CorpusDoc, GoldenTurn, RagCorpus, RagGolden

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "domainrag"
_LABELED = "_repo/BCM/labeled_data"

# subset name → path relative to labeled_data
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
    """answers looks like [["汉语","法语","英语"]] (a list of lists); the first variant is taken
    and joined."""
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
    """corpus/.../json_output/*.json (plain text; the html_output duplicates are skipped)."""
    recs: list[dict] = []
    for f in sorted(corpus_root.rglob("json_output/*.json")):
        try:
            recs.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return recs


def build_corpus(corpus_recs: list[dict]) -> tuple[RagCorpus, dict[str, str], dict[str, str]]:
    """corpus records → RagCorpus, with title → id / url → id lookup tables (for references
    without an id)."""
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
    """Reference records (dict or list[dict]) → list of gold doc ids; those without an id are
    looked up by title / url."""
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
# QA normalization for each subset
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
    """history_qa (≥0 history turns) + a final coreference follow-up → multi-turn RagGolden."""
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
# Reading and entry point
# ────────────────────────────────────────────────────────────────────────── #


def _read_qa_records(path: Path, subset: str) -> list[dict]:
    if not path.exists():
        # Fallback: rglob by file name (still locates the file when the directory layout changes)
        candidates = list(path.parent.parent.rglob(path.name))
        if not candidates:
            raise FileNotFoundError(f"未找到 {path.name}（运行 make eval-dataset）")
        path = candidates[0]
    text = path.read_text(encoding="utf-8")
    # The data files are a mix of single-line jsonl records and pretty-printed JSON objects
    # (basic_qa has 129 single-line + a few multi-line records in practice); everything is parsed
    # object by object with raw_decode, which handles both formats.
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
    """Load a DomainRAG subset: corpus/ + labeled_data/<subset> → (RagCorpus, [RagGolden]).

    Raises FileNotFoundError when something is missing; conftest catches it and the session
    fixture skips.
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
    """Pure normalization: raw record → schema (the deterministic core, covered by unit tests)."""
    corpus, by_title, by_url = build_corpus(corpus_recs)
    parser = _SUBSET_PARSERS.get(subset, _parse_basic)
    goldens = [parser(q, by_title, by_url) for q in qa_recs]
    return corpus, goldens
