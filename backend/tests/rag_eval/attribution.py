"""Claim-level attribution (a lightweight take on RAGChecker): is a wrong answer a retrieval
problem or a generation problem?

Split the answer into atomic sentences (claims) and decide for each one whether the
retrieved context supports it (lexical overlap, same family as the faithfulness heuristic
in ``evaluation.py``, no LLM). Unsupported claims are then attributed by whether the gold
document is already among the retrieved results:

- ``retrieval_miss``: the gold document was not retrieved → fix retrieval first
  (chunking / recall / rerank)
- ``generation_error``: the gold document is already in the context and the answer is
  still wrong → fix generation (prompt / model)

The result goes into the ``attribution`` field of the experiment report so that a sweep
comparison can answer "did the gain come from retrieval or from generation".
"""
from __future__ import annotations

import re

from porto_chatbot.embeddings import tokens

_SUPPORT_THRESHOLD = 0.35  # matches src evaluation._SENTENCE_SUPPORT_THRESHOLD
_CLAIM_SPLIT = re.compile(r"[。！？!?\n；;]+")


def split_claims(answer: str) -> list[str]:
    """Answer → list of atomic sentences (split on Chinese and English sentence punctuation)."""
    return [s.strip() for s in _CLAIM_SPLIT.split(answer) if len(s.strip()) >= 4]


def _support(claim: str, context: str) -> float:
    c = set(tokens(claim))
    if not c:
        return 0.0
    x = set(tokens(context))
    return len(c & x) / len(c)


def attribute_answer(
    answer: str,
    contexts: list[str],
    gold_doc_ids: list[str],
    retrieved_doc_ids: list[str],
) -> dict:
    """Claim-level attribution for one answer.

    Returns
    -------
    dict
        ``n_claims / n_supported / n_unsupported / generation_errors /
        retrieval_misses / per_claim: [{text, supported, attribution}]``.
        Without a gold annotation (gold_doc_ids is empty) every unsupported claim is counted
        as ``retrieval_miss``, an upper bound (``unknown_gold=True``); for reference only, not
        gated.
    """
    context = "\n".join(contexts)
    gold_in_context = bool(set(gold_doc_ids) & set(retrieved_doc_ids))
    unknown_gold = not gold_doc_ids

    per_claim: list[dict] = []
    n_gen = n_ret = 0
    for claim in split_claims(answer):
        if _support(claim, context) >= _SUPPORT_THRESHOLD:
            per_claim.append({"text": claim[:80], "supported": True, "attribution": None})
            continue
        if unknown_gold:
            kind = "retrieval_miss"
            n_ret += 1
        elif gold_in_context:
            kind = "generation_error"  # the gold is at hand, yet it answered with unsupported content
            n_gen += 1
        else:
            kind = "retrieval_miss"  # the gold was not retrieved
            n_ret += 1
        per_claim.append({"text": claim[:80], "supported": False, "attribution": kind})

    n_claims = len(per_claim)
    n_supported = sum(1 for c in per_claim if c["supported"])
    n_unsupported = n_claims - n_supported
    return {
        "n_claims": n_claims,
        "n_supported": n_supported,
        "n_unsupported": n_unsupported,
        "generation_errors": n_gen,
        "retrieval_misses": n_ret,
        "unknown_gold": unknown_gold,
        "gold_in_context": gold_in_context,
        "per_claim": per_claim,
    }


def aggregate_attribution(per_case: list[dict]) -> dict:
    """Aggregate across cases: the two error rates plus the unsupported-claim rate (rate =
    count / total claims)."""
    tot = sum(c.get("n_claims", 0) for c in per_case)
    if tot == 0:
        return {
            "n_claims": 0, "unsupported_rate": 0.0,
            "generation_error_rate": 0.0, "retrieval_miss_rate": 0.0,
        }
    unsup = sum(c.get("n_unsupported", 0) for c in per_case)
    gen = sum(c.get("generation_errors", 0) for c in per_case)
    ret = sum(c.get("retrieval_misses", 0) for c in per_case)
    return {
        "n_claims": tot,
        "unsupported_rate": round(unsup / tot, 4),
        "generation_error_rate": round(gen / tot, 4),
        "retrieval_miss_rate": round(ret / tot, 4),
    }
