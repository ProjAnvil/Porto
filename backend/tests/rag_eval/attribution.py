"""claim 级归因（RAGChecker 思路的轻量实现）——回答错在检索还是生成。

把回答拆成原子句（claim），逐句判定是否被检索上下文支持（词面重叠，与
``evaluation.py`` 的 faithfulness 启发式同族，零 LLM）。不支持的句子再按
gold 文档是否已在检索结果中二分归因：

- ``retrieval_miss``  —— gold 文档没检回：先修检索（chunk/召回/rerank）
- ``generation_error``—— gold 文档已在上下文里仍然答飞：修生成（prompt/模型）

输出进实验报告的 ``attribution`` 字段，让 sweep 对比能回答「提升来自检索
还是生成」。
"""
from __future__ import annotations

import re

from porto_chatbot.embeddings import tokens

_SUPPORT_THRESHOLD = 0.35  # 与 src evaluation._SENTENCE_SUPPORT_THRESHOLD 对齐
_CLAIM_SPLIT = re.compile(r"[。！？!?\n；;]+")


def split_claims(answer: str) -> list[str]:
    """回答 → 原子句列表（按中英文句读切分）。"""
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
    """对单条回答做 claim 级归因。

    Returns
    -------
    dict
        ``n_claims / n_supported / n_unsupported / generation_errors /
        retrieval_misses / per_claim: [{text, supported, attribution}]``。
        无 gold 标注（gold_doc_ids 为空）时不支持句一律记 ``retrieval_miss``
        的上界（``unknown_gold=True``），供参考不进门禁。
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
            kind = "generation_error"  # gold 已在手边，仍答了无支持的内容
            n_gen += 1
        else:
            kind = "retrieval_miss"  # gold 未检回
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
    """跨 case 聚合：两类错误率 + 不支持句率（rate = 计数 / 总 claim 数）。"""
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
