"""RAG retrieval-optimization experiment runner: build the KB for a profile → run RAG →
deepeval judge → write the report.

A pytest-free runner built from plain functions; the pytest gates (test_rag_gate.py, ...)
and this CLI both call ``run_experiment()``.

Dual-track evaluation (2026-08 improvement)::

    --track retrieval  Retrieval track: retrieval only; recall@k/MRR/hit@k from gold_doc_ids,
                        no LLM, seconds to run → hard PR gate (test_retrieval_gate.py)
    --track e2e        End-to-end track: retrieval → generation → the six LLM-judge metrics
                        + refusal + attribution (nightly, soft gate via RAG_EVAL_REPORT_ONLY)

CLI usage::

    # list every profile / dataset
    python -m tests.rag_eval.experiment --list
    python -m tests.rag_eval.experiment --list-datasets

    # retrieval track (no LLM, fast regression check)
    python -m tests.rag_eval.experiment baseline --track retrieval
    python -m tests.rag_eval.experiment baseline --dataset domainrag:multidoc --track retrieval

    # end to end (the first 20 cases by default)
    python -m tests.rag_eval.experiment rerank
    python -m tests.rag_eval.experiment baseline --dataset conversation --max-cases 10

    # refusal evaluation (generation + rule verdict, no judge LLM)
    python -m tests.rag_eval.experiment baseline --dataset refusal

    # sweep several profiles / compare past reports
    python -m tests.rag_eval.experiment --sweep baseline rerank semantic
    python -m tests.rag_eval.experiment --compare reports/baseline_*.json reports/rerank_*.json
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from porto_chatbot.llm.client import LLMClient
from porto_chatbot.models.enums import QueryTransformStrategy
from porto_chatbot.query_transform import retrieve_with_transform
from porto_chatbot.settings import Settings

from .attribution import aggregate_attribution, attribute_answer
from .loaders import available_datasets, load_dataset
from .metrics import (
    MAX_ERROR_RATE,
    aggregate,
    build_metrics,
    evaluate_case,
    judge,
    judge_latency,
    latency_summary,
)
from .profiles import PROFILES, get_profile
from .provision import build_eval_kb
from .refusal_detector import REFUSAL_ACCURACY_THRESHOLD, refusal_verdict
from .retrieval_metrics import (
    aggregate_retrieval,
    doc_id_map,
    judge_retrieval,
    retrieval_case_metrics,
    retrieved_doc_ids,
)
from .runner import run_rag, run_rag_multiturn
from .schema import EvalProfile

_DIR = Path(__file__).resolve().parent
_ENV_TEST = _DIR.parents[1] / ".env.test"
_REPORTS = _DIR / "reports"


# ────────────────────────────────────────────────────────────────────────── #
# Shared helpers (conftest.py imports and reuses them too)
# ────────────────────────────────────────────────────────────────────────── #


def read_env_test() -> dict[str, str]:
    """Read backend/.env.test and merge it with the process environment; **existing environment
    variables win** (same precedence as described in that file's header), so secrets injected
    directly by CI and RAG_EVAL_JUDGE_* take effect."""
    out: dict[str, str] = dict(os.environ)
    if not _ENV_TEST.exists():
        return out
    for line in _ENV_TEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out.setdefault(k.strip(), v.strip())
    return out


def build_llm(env: dict[str, str]) -> LLMClient:
    # Note: Settings fields carry a validation_alias and populate_by_name is off, so passing
    # field names (agent_api_key=...) is silently ignored by pydantic: they must be passed by
    # alias. (The old implementation passed field names, which never took effect, and only
    # "worked" because Settings happened to read the production .env.)
    settings = Settings(
        LANGCHAIN_AGENT_PROVIDER=env.get("LANGCHAIN_AGENT_PROVIDER", "openai"),
        LANGCHAIN_API_KEY=env["LANGCHAIN_API_KEY"],
        LANGCHAIN_BASE_URL=env.get("LANGCHAIN_BASE_URL") or None,
        LANGCHAIN_MODEL=env.get("LANGCHAIN_MODEL", "gpt-4.1-mini"),
    )
    return LLMClient(settings)


def build_judge_model(env: dict[str, str]):
    """Build the deepeval judge LLM instance: its own config + an explicit base_url + temperature
    0.

    - Independence: ``RAG_EVAL_JUDGE_PROVIDER/MODEL/API_KEY/BASE_URL`` wins, falling back to
      LANGCHAIN_* (the same config as the generator). "A strong model judges, a cheap model
      generates" is configured here.
    - 401 trap: for a string model name deepeval 4.x falls back to ``GPTModel`` without passing
      base_url, and a self-hosted endpoint then always answers 401, hence the explicit instance.
    - Determinism: temperature 0 (judge jitter is the main reason metrics near a threshold
      flake).
    """
    from deepeval.models.llms.openai_model import GPTModel

    provider = env.get("RAG_EVAL_JUDGE_PROVIDER") or env.get("LANGCHAIN_AGENT_PROVIDER", "openai")
    model_name = env.get("RAG_EVAL_JUDGE_MODEL") or env.get("LANGCHAIN_MODEL", "gpt-4.1-mini")
    api_key = env.get("RAG_EVAL_JUDGE_API_KEY") or env["LANGCHAIN_API_KEY"]
    base_url = env.get("RAG_EVAL_JUDGE_BASE_URL") or env.get("LANGCHAIN_BASE_URL") or None

    if provider == "anthropic":
        from deepeval.models.llms.anthropic_model import AnthropicModel

        return AnthropicModel(model=model_name, api_key=api_key)
    try:
        return GPTModel(model=model_name, api_key=api_key, base_url=base_url, temperature=0.0)
    except TypeError:  # older GPTModel has no temperature parameter
        return GPTModel(model=model_name, api_key=api_key, base_url=base_url)


def precheck(
    env: dict[str, str],
    *,
    need_judge: bool = True,
    need_generation: bool | None = None,
) -> tuple[LLMClient | None, object | None]:
    """Precheck that the generation LLM (+ the judge LLM when asked for) is usable; raises
    RuntimeError when it is unreachable.

    - ``need_generation`` follows ``need_judge`` by default. The retrieval track passes
      ``need_judge=False``: with a pure NONE strategy and no rerank, pass ``need_generation=False``
      → it runs with zero credentials (returning ``(None, None)``); with a transform or rerank
      on, the generation LLM is still required.
    """
    if need_generation is None:
        need_generation = need_judge
    llm = None
    if need_generation:
        if not env.get("LANGCHAIN_API_KEY"):
            raise RuntimeError("无 LANGCHAIN_API_KEY（.env.test）")
        llm = build_llm(env)
        if not llm.enabled:
            raise RuntimeError("LLM 未启用")
        llm.complete("ping", user="reply: ok")
    if not need_judge:
        return llm, None
    # Fallback for instantiating deepeval metrics
    os.environ["OPENAI_API_KEY"] = env["LANGCHAIN_API_KEY"]
    judge_model = build_judge_model(env)
    from deepeval.test_case import LLMTestCase

    build_metrics(judge_model)["faithfulness"].measure(
        LLMTestCase(input="x", actual_output="y", retrieval_context=["z"])
    )
    return llm, judge_model


# ────────────────────────────────────────────────────────────────────────── #
# Aggregation helpers
# ────────────────────────────────────────────────────────────────────────── #


def _by_category(case_rows: list[dict], metric_keys: list[str]) -> dict[str, dict]:
    """Mean of each metric grouped by category (the category dimension lets the report locate
    weak spots)."""
    groups: dict[str, list[dict]] = {}
    for row in case_rows:
        groups.setdefault(row.get("category", "default"), []).append(row)
    out: dict[str, dict] = {}
    for cat, rows in sorted(groups.items()):
        entry: dict = {"n": len(rows)}
        for k in metric_keys:
            vals = [r["scores"].get(k) for r in rows if r["scores"].get(k) is not None]
            if vals:
                entry[k] = round(sum(vals) / len(vals), 4)
        out[cat] = entry
    return out


# ────────────────────────────────────────────────────────────────────────── #
# Core runner
# ────────────────────────────────────────────────────────────────────────── #


def run_experiment(
    profile: EvalProfile,
    *,
    env: dict[str, str] | None = None,
    output_dir: Path | None = None,
    dataset: str = "domainrag",
    track: str = "e2e",
) -> dict:
    """Run the RAG eval for ``profile`` and produce the report (written to
    ``output_dir/<profile>_<ts>.json``).

    Parameters
    ----------
    dataset:
        Dataset name (see ``loaders.available_datasets``; domainrag extractive by default).
    track:
        ``retrieval`` (deterministic retrieval metrics, no LLM) or ``e2e`` (retrieval → generation
        → judge).
    """
    from tempfile import mkdtemp

    env = env or read_env_test()
    output_dir = output_dir or _REPORTS
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  PROFILE: {profile.name} | DATASET: {dataset} | TRACK: {track}")
    print(f"  {profile.description}")
    print(f"  chunk={profile.max_chunk_chars} embed={profile.embedding_provider}/"
          f"{profile.embedding_model or 'n/a'} rerank={profile.rerank_enabled}/"
          f"top{profile.rerank_top_n if profile.rerank_enabled else '-'} "
          f"strategy={profile.strategy} top_k={profile.top_k} "
          f"backend={profile.vector_backend}")
    print(f"{'='*60}")

    corpus, goldens_all = load_dataset(dataset)
    goldens = goldens_all[: profile.max_cases] if profile.max_cases > 0 else goldens_all
    if not goldens:
        raise RuntimeError(f"数据集 {dataset} 无 golden（max_cases={profile.max_cases}）")
    print(f"  数据集: {len(corpus.docs)} docs, {len(goldens)} goldens")
    id_map = doc_id_map(corpus)

    # Retrieval track: no LLM with the NONE strategy and no rerank; otherwise retrieval itself
    # needs an LLM (transform / rerank / condense)
    has_multiturn = any(g.turns for g in goldens)
    need_llm_for_retrieval = (
        profile.strategy != QueryTransformStrategy.NONE
        or profile.rerank_enabled
        or (profile.condense_history and has_multiturn)
    )
    if track == "retrieval":
        llm, judge_model = precheck(
            env, need_judge=False, need_generation=need_llm_for_retrieval
        )
        if llm is None:
            print("  检索轨零 LLM 模式（strategy=NONE, rerank=off）")
    else:
        llm, judge_model = precheck(env, need_judge=True)

    t0 = time.perf_counter()
    tmp = Path(mkdtemp(prefix=f"eval_{profile.name}_"))
    eval_kb = build_eval_kb(corpus, tmp, profile, env=env)
    print(f"  索引构建: {time.perf_counter() - t0:.1f}s")

    strategy = profile.strategy
    metrics = build_metrics(judge_model) if (track == "e2e" and judge_model is not None) else {}

    per_case: list[dict[str, float]] = []       # per-case e2e judge metrics
    retrieval_case: list[dict[str, float]] = []  # deterministic retrieval metrics (computed on both tracks)
    case_reports: list[dict] = []
    timings: list[dict] = []
    errored: list[str] = []
    refusal_rows: list[dict] = []
    attribution_rows: list[dict] = []

    def _record_retrieval(gold_ids: list[str], chunks: list) -> dict[str, float]:
        # Without a gold annotation (refusal negatives / goldens whose gold id could not be resolved)
        # retrieval quality cannot be measured, so they are left out of the retrieval track; otherwise
        # a recall that is always 0 would make a refusal experiment look like a retrieval regression.
        if not gold_ids:
            return {}
        scores = retrieval_case_metrics(gold_ids, retrieved_doc_ids(chunks, id_map))
        retrieval_case.append(scores)
        return scores

    def _retrieve(question: str, history: list[tuple[str, str]] | None = None):
        """Retrieval entry point shared by the retrieval track and e2e. history takes effect through
        the production condense gate node (settings.condense_enabled is injected from the profile;
        the LLM rewrites only when it decides it is needed)."""
        t0 = time.perf_counter()
        result = retrieve_with_transform(
            question, strategy, eval_kb.store, eval_kb.settings, llm, profile.top_k,
            history=history,
        )
        return result, round(time.perf_counter() - t0, 2)

    for idx, g in enumerate(goldens):
        is_multiturn = g.turns is not None and len(g.turns) > 1
        try:
            if track == "retrieval":
                if is_multiturn:
                    # Multi-turn goldens are flattened: each turn is retrieved on its own. With condense on, the
                    # earlier turns' questions + reference answers stand in for the production conversation history
                    # that is passed to the production gate node (the retrieval track does no generation, and the
                    # golden answer is a reasonable stand-in for "what the assistant would have said last turn").
                    sim_history: list[tuple[str, str]] = []
                    for ti, turn in enumerate(g.turns):
                        result, dt = _retrieve(turn.question, history=sim_history or None)
                        scores = _record_retrieval(turn.gold_doc_ids, result.chunks)
                        case_reports.append({
                            "question": f"[t{ti}] {turn.question}",
                            "query": result.condensed_query or turn.question,
                            "condense_applied": result.condense_applied,
                            "category": g.category,
                            "scores": {},
                            "retrieval": scores,
                            "degraded": result.degraded,
                            "timing": {"retrieve_s": dt},
                        })
                        timings.append({"retrieve_s": dt})
                        sim_history.append((turn.question, turn.reference_answer))
                else:
                    result, dt = _retrieve(g.question)
                    scores = _record_retrieval(g.gold_doc_ids, result.chunks)
                    case_reports.append({
                        "question": g.question,
                        "category": g.category,
                        "scores": {},
                        "retrieval": scores,
                        "degraded": result.degraded,
                        "timing": {"retrieve_s": dt},
                    })
                    timings.append({"retrieve_s": dt})
                retr_print = (case_reports[-1]['retrieval'] or {}).get('recall_at_k')
                print(f"  [{idx+1}/{len(goldens)}] "
                      + (f"recall={retr_print:.2f} " if retr_print is not None else "")
                      + f"| {g.question[:30]}")
                continue

            # ── e2e track ──
            if is_multiturn:
                mt = run_rag_multiturn(g, eval_kb, llm, strategy=strategy, top_k=profile.top_k)
                turn_retr = [
                    _record_retrieval(turn.gold_doc_ids, rec.chunks)
                    for turn, rec in zip(g.turns, mt.turns, strict=True)
                ]
                t0 = time.perf_counter()
                scores = evaluate_case(mt.test_case, metrics) if metrics else {}
                judge_s = round(time.perf_counter() - t0, 2)
                case_reports.append({
                    "question": g.question,
                    "category": g.category,
                    "scores": scores,
                    "retrieval": turn_retr[-1],
                    "turns": [
                        {
                            "question": r.question,
                            "query": r.condensed or r.question,
                            "answer": r.answer[:120],
                            "retrieval": tr,
                        }
                        for r, tr in zip(mt.turns, turn_retr, strict=True)
                    ],
                    "degraded": mt.transform_result.degraded,
                    "timing": {"retrieve_s": mt.timing["retrieve_s_mean"],
                               "generate_s": mt.timing["generate_s_mean"],
                               "judge_s": judge_s, "n_turns": mt.timing["n_turns"]},
                    "answer": mt.test_case.actual_output[:200],
                })
                timings.append({"retrieve_s": mt.timing["retrieve_s_mean"],
                                "generate_s": mt.timing["generate_s_mean"]})
                attribution_rows.append(attribute_answer(
                    mt.test_case.actual_output,
                    mt.test_case.retrieval_context,
                    g.gold_doc_ids,
                    retrieved_doc_ids(mt.transform_result.chunks, id_map),
                ))
                per_case.append(scores)
                final_recall = (turn_retr[-1] or {}).get("recall_at_k")
                print(f"  [{idx+1}/{len(goldens)}] turns={mt.timing['n_turns']} "
                      + (f"recall={final_recall:.2f} " if final_recall is not None else "")
                      + f"| {g.question[:30]}")
                continue

            tc, result, timing = run_rag(g, eval_kb, llm, strategy=strategy, top_k=profile.top_k)
            retr_scores = _record_retrieval(g.gold_doc_ids, result.chunks)

            if g.expected_behavior == "refuse":
                # Refusal sample: rule verdict (no judge LLM), not included in the six-metric means
                v = refusal_verdict(tc.actual_output, g.expected_behavior)
                refusal_rows.append({"question": g.question, "correct": v,
                                     "refused": v > 0, "answer": tc.actual_output[:120]})
                case_reports.append({
                    "question": g.question, "category": g.category, "scores": {},
                    "retrieval": retr_scores, "refusal": v, "degraded": result.degraded,
                    "timing": timing, "answer": tc.actual_output[:200],
                })
                timings.append({"retrieve_s": timing["retrieve_s"], "generate_s": timing["generate_s"]})
                print(f"  [{idx+1}/{len(goldens)}] refusal={'✓' if v else '✗'} | {g.question[:30]}")
                continue

            t0 = time.perf_counter()
            scores = evaluate_case(tc, metrics) if metrics else {}
            timing["judge_s"] = round(time.perf_counter() - t0, 2)
            attribution_rows.append(attribute_answer(
                tc.actual_output, tc.retrieval_context, g.gold_doc_ids,
                retrieved_doc_ids(result.chunks, id_map),
            ))
            # Over-refusal diagnostic: the rate of wrongly refused answerable samples (reported only,
            # not gated)
            over_refused = refusal_verdict(tc.actual_output, "answer") == 0.0
        except Exception as e:  # noqa: BLE001
            errored.append(f"case#{idx} {g.question[:30]}: {e}")
            print(f"  [{idx+1}/{len(goldens)}] ❌ {e}")
            continue

        per_case.append(scores)
        row = {
            "question": g.question,
            "category": g.category,
            "scores": scores,
            "retrieval": retr_scores,
            "degraded": result.degraded,
            "timing": timing,
            "answer": tc.actual_output[:200],
            "over_refused": over_refused,
        }
        case_reports.append(row)
        timings.append({"retrieve_s": timing["retrieve_s"], "generate_s": timing["generate_s"]})
        recall_v = (retr_scores or {}).get("recall_at_k")
        print(f"  [{idx+1}/{len(goldens)}] cv={scores.get('contextual_relevancy', 0):.2f} "
              f"ac={scores.get('answer_correctness', 0):.2f} "
              + (f"recall={recall_v:.2f} " if recall_v is not None else "")
              + f"chunks={timing['n_chunks']} "
              f"judge={timing.get('judge_s', 0):.0f}s | {g.question[:30]}")

    # ── Aggregation and gating ──
    if retrieval_case:
        retr_mean = aggregate_retrieval(retrieval_case)
        retr_passed, retr_detail = judge_retrieval(retr_mean)
    else:
        # No golden has a gold annotation (e.g. the refusal set): the retrieval track cannot be
        # measured and does not block
        retr_mean, retr_passed, retr_detail = {}, True, {}
    latency = latency_summary(timings)
    latency_passed, latency_detail = judge_latency(latency)

    passed = retr_passed and latency_passed
    mean: dict[str, float] = {}
    detail: dict[str, dict] = {}
    if track == "e2e" and per_case and metrics:
        mean = aggregate(per_case)
        judge_passed, detail = judge(mean)
        passed = passed and judge_passed

    refusal_summary: dict | None = None
    if refusal_rows:
        acc = round(sum(r["correct"] for r in refusal_rows) / len(refusal_rows), 4)
        refusal_summary = {"n": len(refusal_rows), "accuracy": acc,
                           "threshold": REFUSAL_ACCURACY_THRESHOLD,
                           "passed": acc >= REFUSAL_ACCURACY_THRESHOLD}
        passed = passed and refusal_summary["passed"]

    attribution_summary = aggregate_attribution(attribution_rows) if attribution_rows else None
    over_refusal_rate = round(
        sum(1 for c in case_reports if c.get("over_refused")) /
        max(len([c for c in case_reports if "over_refused" in c]), 1), 4
    ) if any("over_refused" in c for c in case_reports) else None

    error_rate = len(errored) / max(len(goldens), 1)
    invalid = error_rate > MAX_ERROR_RATE
    if invalid:
        passed = False

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    report = {
        "profile": profile.name,
        "dataset": dataset,
        "track": track,
        "description": profile.description,
        "config": asdict(profile),
        "timestamp": timestamp,
        "n": len(goldens),
        "n_success": len(case_reports),
        "passed": passed,
        "invalid": invalid,          # errored > MAX_ERROR_RATE → the means cannot be trusted
        "error_rate": round(error_rate, 4),
        "mean": mean,
        "detail": detail,
        "retrieval": {"mean": retr_mean, "detail": retr_detail, "passed": retr_passed},
        "latency": latency,
        "latency_detail": latency_detail,
        "latency_passed": latency_passed,
        "refusal": refusal_summary,
        "attribution": attribution_summary,
        "over_refusal_rate": over_refusal_rate,
        "by_category": _by_category(case_reports, list(retr_mean) + list(mean)),
        "cases": case_reports,
        "errored": errored,
    }
    out_file = output_dir / f"{profile.name}_{timestamp}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  报告: {out_file}")
    if retr_mean:
        print(f"  retrieval: {', '.join(f'{k}={v:.3f}' for k, v in retr_mean.items())}")
    if mean:
        print(f"  e2e mean : {', '.join(f'{k}={v:.3f}' for k, v in mean.items())}")
    if refusal_summary:
        print(f"  refusal  : accuracy={refusal_summary['accuracy']:.2f} "
              f"(thr={REFUSAL_ACCURACY_THRESHOLD}) passed={refusal_summary['passed']}")
    if attribution_summary:
        a = attribution_summary
        print(f"  归因     : gen_err={a['generation_error_rate']:.2%} "
              f"retr_miss={a['retrieval_miss_rate']:.2%}")
    print(f"  latency  : {latency} passed={latency_passed}")
    print(f"  总判定   : passed={passed} invalid={invalid}"
          + (f"（errored={len(errored)}）" if errored else ""))
    return report


# ────────────────────────────────────────────────────────────────────────── #
# Report comparison
# ────────────────────────────────────────────────────────────────────────── #


def compare_reports(report_paths: list[Path]) -> None:
    """Compare the key metrics of several reports side by side (e2e metrics + retrieval-track
    metrics + refusal + attribution)."""
    reports = []
    for p in report_paths:
        reports.append(json.loads(Path(p).read_text(encoding="utf-8")))

    rows: list[tuple[str, str, str]] = [
        ("faithfulness", "mean.faithfulness", ".3f"),
        ("answer_relevancy", "mean.answer_relevancy", ".3f"),
        ("answer_correctness", "mean.answer_correctness", ".3f"),
        ("contextual_precision", "mean.contextual_precision", ".3f"),
        ("contextual_recall", "mean.contextual_recall", ".3f"),
        ("contextual_relevancy", "mean.contextual_relevancy", ".3f"),
        ("recall_at_k", "retrieval.mean.recall_at_k", ".3f"),
        ("mrr", "retrieval.mean.mrr", ".3f"),
        ("hit_at_k", "retrieval.mean.hit_at_k", ".3f"),
        ("noise_ratio", "retrieval.mean.noise_ratio", ".3f"),
    ]

    def _get(r: dict, path: str):
        cur: Any = r
        for part in path.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

    names = [f"{r['profile']}/{r.get('dataset', '?')}" for r in reports]
    hdr = f"{'metric':<24}" + "".join(f"{n:>18}" for n in names)
    print(f"\n{'='*len(hdr)}")
    print(hdr)
    print("-" * len(hdr))
    for m, path, fmt in rows:
        vals = [_get(r, path) for r in reports]
        if all(v is None for v in vals):
            continue
        cells = [
            f"{v:>18{fmt}}" if isinstance(v, (int, float)) else f"{'—':>18}"
            for v in vals
        ]
        row = f"{m:<24}" + "".join(cells)
        nums = [v for v in vals if isinstance(v, (int, float))]
        if len(nums) > 1 and nums[0] is not None and vals[0] is not None:
            deltas = [v - nums[0] for v in nums[1:]]
            row += "  " + " ".join(f"{'+' if d >= 0 else ''}{d:.3f}" for d in deltas)
        print(row)
    # Refusal / attribution / latency / verdict
    if any(r.get("refusal") for r in reports):
        print(f"{'refusal_accuracy':<24}" + "".join(
            f"{(r['refusal'] or {}).get('accuracy', '—'):>18}" for r in reports))
    if any(r.get("attribution") for r in reports):
        print(f"{'gen_error_rate':<24}" + "".join(
            f"{(r['attribution'] or {}).get('generation_error_rate', '—'):>18}" for r in reports))
        print(f"{'retrieval_miss_rate':<24}" + "".join(
            f"{(r['attribution'] or {}).get('retrieval_miss_rate', '—'):>18}" for r in reports))
    print(f"{'latency_p95(r/g)':<24}" + "".join(
        f"{'/'.join(str((r.get('latency') or {}).get(k, {}).get('p95', '—')) for k in ('retrieve_s', 'generate_s')):>18}"
        for r in reports))
    print(f"{'n_success':<24}" + "".join(f"{r.get('n_success', '?'):>18}" for r in reports))
    print(f"{'passed':<24}" + "".join(f"{str(r.get('passed')):>18}" for r in reports))
    print(f"{'='*len(hdr)}\n")


# ────────────────────────────────────────────────────────────────────────── #
# CLI
# ────────────────────────────────────────────────────────────────────────── #


def main():
    parser = argparse.ArgumentParser(
        prog="python -m tests.rag_eval.experiment",
        description="RAG 检索优化实验框架——双轨评估（retrieval/e2e）+ 对比报告",
    )
    parser.add_argument("profile", nargs="?", help="要跑的 profile 名（如 baseline / rerank）")
    parser.add_argument("--list", action="store_true", help="列出所有可用 profile")
    parser.add_argument("--list-datasets", action="store_true", help="列出所有可用数据集")
    parser.add_argument("--dataset", default="domainrag",
                        help="数据集名（默认 domainrag；见 --list-datasets）")
    parser.add_argument("--track", choices=["retrieval", "e2e"], default="e2e",
                        help="retrieval=零 LLM 检索轨；e2e=端到端 judge 轨")
    parser.add_argument("--max-cases", type=int, help="覆盖 profile.max_cases")
    parser.add_argument("--top-k", type=int, help="覆盖 profile.top_k")
    parser.add_argument("--chunk-size", type=int, help="覆盖 profile.max_chunk_chars")
    parser.add_argument(
        "--strategy",
        choices=[s.value for s in QueryTransformStrategy],
        help="覆盖 profile.strategy",
    )
    parser.add_argument("--sweep", nargs="+", metavar="PROFILE", help="批量跑多个 profile")
    parser.add_argument("--sweep-all", action="store_true", help="跑所有 profile")
    parser.add_argument("--compare", nargs="+", metavar="JSON", help="对比指定报告文件")
    args = parser.parse_args()

    # --- list ---
    if args.list:
        print(f"\n{'name':<18} {'description'}")
        print("-" * 70)
        for name, p in sorted(PROFILES.items()):
            print(f"{name:<18} {p.description}")
        print()
        return
    if args.list_datasets:
        print("\n可用数据集：")
        for name in available_datasets():
            print(f"  {name}")
        print()
        return

    # --- compare ---
    if args.compare:
        compare_reports([Path(p) for p in args.compare])
        return

    # --- sweep ---
    if args.sweep_all:
        names = sorted(PROFILES)
    elif args.sweep:
        names = args.sweep
    else:
        names = []

    if names:
        latest = {}
        for name in names:
            profile = get_profile(name)
            if args.max_cases:
                profile.max_cases = args.max_cases
            run_experiment(profile, dataset=args.dataset, track=args.track)
        # Automatic comparison: take the latest report of each profile
        for f in _REPORTS.glob("*.json"):
            data = json.loads(f.read_text(encoding="utf-8"))
            pn = data.get("profile", "")
            if pn in names and data.get("dataset") == args.dataset \
                    and (pn not in latest or f.stat().st_mtime > latest[pn].stat().st_mtime):
                latest[pn] = f
        if len(latest) > 1:
            print("\n\n========== SWEEP 对比 ==========")
            compare_reports([latest[n] for n in names if n in latest])
        return

    # --- single ---
    if not args.profile:
        parser.print_help()
        return

    profile = get_profile(args.profile)
    if args.max_cases:
        profile.max_cases = args.max_cases
    if args.top_k:
        profile.top_k = args.top_k
    if args.chunk_size:
        profile.max_chunk_chars = args.chunk_size
    if args.strategy:
        profile.strategy = QueryTransformStrategy(args.strategy)

    run_experiment(profile, dataset=args.dataset, track=args.track)


if __name__ == "__main__":
    main()
