"""RAG 检索优化实验编排器——按 profile 构建 KB → 跑 RAG → deepeval 裁判 → 出报告。

剥离 pytest 的纯函数 runner；pytest 门禁（test_rag_gate.py 等）和本 CLI 都调
``run_experiment()``。

双轨评估（2026-08 改进）::

    --track retrieval  检索轨：只跑检索，用 gold_doc_ids 算 recall@k/MRR/hit@k，
                        零 LLM、秒级 → PR 硬门禁（test_retrieval_gate.py）
    --track e2e        端到端轨：检索→生成→LLM judge 六指标 + 拒答 + 归因
                        （nightly，RAG_EVAL_REPORT_ONLY 软门禁）

CLI 用法::

    # 列出所有 profile / 数据集
    python -m tests.rag_eval.experiment --list
    python -m tests.rag_eval.experiment --list-datasets

    # 检索轨（零 LLM，快速回归）
    python -m tests.rag_eval.experiment baseline --track retrieval
    python -m tests.rag_eval.experiment baseline --dataset domainrag:multidoc --track retrieval

    # 端到端（默认前 20 case）
    python -m tests.rag_eval.experiment rerank
    python -m tests.rag_eval.experiment baseline --dataset conversation --max-cases 10

    # 拒答评估（生成 + 规则判定，无 judge LLM）
    python -m tests.rag_eval.experiment baseline --dataset refusal

    # 批量扫多个 profile / 对比历史报告
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
# 共享 helper（conftest.py 也 import 复用）
# ────────────────────────────────────────────────────────────────────────── #


def read_env_test() -> dict[str, str]:
    """读 backend/.env.test 并与进程环境合并；**已有环境变量优先**（与该文件
    头注释的优先级一致），CI 直接注入的 secrets / RAG_EVAL_JUDGE_* 由此生效。"""
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
    # 注意：Settings 字段带 validation_alias 且未开 populate_by_name，按字段名
    # （agent_api_key=...）传参会被 pydantic 静默忽略——必须按 alias 传。
    # （旧实现按字段名传参从未生效，一直靠 Settings 读生产 .env「碰巧」工作。）
    settings = Settings(
        LANGCHAIN_AGENT_PROVIDER=env.get("LANGCHAIN_AGENT_PROVIDER", "openai"),
        LANGCHAIN_API_KEY=env["LANGCHAIN_API_KEY"],
        LANGCHAIN_BASE_URL=env.get("LANGCHAIN_BASE_URL") or None,
        LANGCHAIN_MODEL=env.get("LANGCHAIN_MODEL", "gpt-4.1-mini"),
    )
    return LLMClient(settings)


def build_judge_model(env: dict[str, str]):
    """构造 deepeval judge LLM 实例——独立配置 + 显式 base_url + 温度 0。

    - 独立性：优先 ``RAG_EVAL_JUDGE_PROVIDER/MODEL/API_KEY/BASE_URL``（缺省回退
      LANGCHAIN_*，即与 generator 同配置）。「强模型评审、便宜模型生成」在此配置。
    - 401 陷阱：deepeval 4.x 对字符串模型名 fallback ``GPTModel`` 不传 base_url，
      自建端点必 401 —— 因此显式构造实例。
    - 确定性：温度 0（judge 抖动是阈值边缘指标 flaky 的主因）。
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
    except TypeError:  # 旧版本 GPTModel 无 temperature 参数
        return GPTModel(model=model_name, api_key=api_key, base_url=base_url)


def precheck(
    env: dict[str, str],
    *,
    need_judge: bool = True,
    need_generation: bool | None = None,
) -> tuple[LLMClient | None, object | None]:
    """预检生成 LLM（+ 按需 judge LLM）可用；不可达时 raise RuntimeError。

    - ``need_generation`` 缺省跟随 ``need_judge``。检索轨传 ``need_judge=False``：
      纯 NONE 策略且无 rerank 时传 ``need_generation=False`` → 零密钥可跑
      （返回 ``(None, None)``）；开了变换/rerank 则仍需生成 LLM。
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
    # deepeval metric 实例化兜底
    os.environ["OPENAI_API_KEY"] = env["LANGCHAIN_API_KEY"]
    judge_model = build_judge_model(env)
    from deepeval.test_case import LLMTestCase

    build_metrics(judge_model)["faithfulness"].measure(
        LLMTestCase(input="x", actual_output="y", retrieval_context=["z"])
    )
    return llm, judge_model


# ────────────────────────────────────────────────────────────────────────── #
# 聚合 helper
# ────────────────────────────────────────────────────────────────────────── #


def _by_category(case_rows: list[dict], metric_keys: list[str]) -> dict[str, dict]:
    """按 category 分组求各指标均值（category 维度让报告能定位弱项）。"""
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
# 核心 runner
# ────────────────────────────────────────────────────────────────────────── #


def run_experiment(
    profile: EvalProfile,
    *,
    env: dict[str, str] | None = None,
    output_dir: Path | None = None,
    dataset: str = "domainrag",
    track: str = "e2e",
) -> dict:
    """按 ``profile`` 跑 RAG eval 并出报告（写入 ``output_dir/<profile>_<ts>.json``）。

    Parameters
    ----------
    dataset:
        数据集名（见 ``loaders.available_datasets``；默认 domainrag extractive）。
    track:
        ``retrieval``（零 LLM 确定性检索指标）或 ``e2e``（检索→生成→judge）。
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

    # 检索轨：NONE 策略且无 rerank 时零 LLM；否则检索本身需要 LLM（变换/rerank/condense）
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

    per_case: list[dict[str, float]] = []       # e2e judge 指标
    retrieval_case: list[dict[str, float]] = []  # 确定性检索指标（两轨都算）
    case_reports: list[dict] = []
    timings: list[dict] = []
    errored: list[str] = []
    refusal_rows: list[dict] = []
    attribution_rows: list[dict] = []

    def _record_retrieval(gold_ids: list[str], chunks: list) -> dict[str, float]:
        # 无 gold 标注（拒答负样本/未解析出 gold id 的 golden）检索质量不可测，
        # 不计入检索轨——否则 recall 恒 0 会把拒答实验误判为检索回归。
        if not gold_ids:
            return {}
        scores = retrieval_case_metrics(gold_ids, retrieved_doc_ids(chunks, id_map))
        retrieval_case.append(scores)
        return scores

    def _retrieve(question: str, history: list[tuple[str, str]] | None = None):
        """检索轨/e2e 共用的检索入口。history 经生产 condense 门控节点生效
        （settings.condense_enabled 由 profile 注入；LLM 判定需要才改写）。"""
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
                    # 多轮 golden 展平：每轮独立检索。condense 开启时，用「前序轮次
                    # 问题 + 参考答案」模拟生产对话历史传入生产门控节点（检索轨不跑
                    # 生成，golden 答案是「助手上一轮会说什么」的合理替身）。
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

            # ── e2e 轨 ──
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
                # 拒答样本：规则判定（零 judge LLM），不进六指标均值
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
            # over-refusal 诊断：应答样本误拒率（只报告不门禁）
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

    # ── 聚合与门禁 ──
    if retrieval_case:
        retr_mean = aggregate_retrieval(retrieval_case)
        retr_passed, retr_detail = judge_retrieval(retr_mean)
    else:
        # 全部 golden 无 gold 标注（如拒答集）：检索轨不可测，不拦截
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
        "invalid": invalid,          # errored > MAX_ERROR_RATE → 均值不可信
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
# 报告对比
# ────────────────────────────────────────────────────────────────────────── #


def compare_reports(report_paths: list[Path]) -> None:
    """并排对比多份报告的核心指标（e2e 指标 + 检索轨指标 + 拒答 + 归因）。"""
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
    # 拒答 / 归因 / 延迟 / 判定
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
        # 自动对比：取每个 profile 的最新报告
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
