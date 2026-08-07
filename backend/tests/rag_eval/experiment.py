"""RAG 检索优化实验编排器——按 profile 构建 KB → 跑 RAG → deepeval 裁判 → 出报告。

剥离 pytest 的纯函数 runner；pytest 门禁（test_rag_gate.py）和本 CLI 都调 ``run_experiment()``。

CLI 用法::

    # 列出所有 profile
    python -m tests.rag_eval.experiment --list

    # 跑单个 profile（默认前 20 case）
    python -m tests.rag_eval.experiment rerank
    python -m tests.rag_eval.experiment baseline --max-cases 5

    # 微调 profile 参数
    python -m tests.rag_eval.experiment rerank --top-k 4 --max-cases 10

    # 批量扫多个 profile
    python -m tests.rag_eval.experiment --sweep baseline rerank semantic

    # 对比历史报告
    python -m tests.rag_eval.experiment --compare reports/baseline_*.json reports/rerank_*.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from porto_chatbot.llm.client import LLMClient
from porto_chatbot.models.enums import QueryTransformStrategy
from porto_chatbot.settings import Settings

from .loaders.domainrag import load_domainrag
from .metrics import aggregate, build_metrics, evaluate_case, judge
from .profiles import PROFILES, get_profile
from .provision import build_eval_kb
from .runner import run_rag
from .schema import EvalProfile

_DIR = Path(__file__).resolve().parent
_ENV_TEST = _DIR.parents[1] / ".env.test"
_REPORTS = _DIR / "reports"


# ────────────────────────────────────────────────────────────────────────── #
# 共享 helper（conftest.py 也 import 复用）
# ────────────────────────────────────────────────────────────────────────── #


def read_env_test() -> dict[str, str]:
    """直读 backend/.env.test（绕过根 conftest 对 LANGCHAIN_* env 的 autouse 隔离）。"""
    out: dict[str, str] = {}
    if not _ENV_TEST.exists():
        return out
    for line in _ENV_TEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def build_llm(env: dict[str, str]) -> LLMClient:
    settings = Settings(
        agent_provider=env.get("LANGCHAIN_AGENT_PROVIDER", "openai"),
        agent_api_key=env["LANGCHAIN_API_KEY"],
        agent_base_url=env.get("LANGCHAIN_BASE_URL") or None,
        agent_model=env.get("LANGCHAIN_MODEL", "gpt-4.1-mini"),
    )
    return LLMClient(settings)


def build_judge_model(env: dict[str, str]):
    """构造 deepeval judge LLM 实例（显式 base_url，绕过字符串 fallback 401 陷阱）。

    deepeval 4.x 的 ``initialize_model`` 对字符串走 ``should_use_*`` 判定，默认 fallback
    到 ``GPTModel`` 但不传 ``base_url`` → 自建 OpenAI-compatible 端点 401。
    直接构造实例（``is_native_model`` → True）是唯一可靠路径。
    """
    from deepeval.models.llms.openai_model import GPTModel

    provider = env.get("LANGCHAIN_AGENT_PROVIDER", "openai")
    model_name = env.get("LANGCHAIN_MODEL", "gpt-4.1-mini")
    api_key = env["LANGCHAIN_API_KEY"]
    base_url = env.get("LANGCHAIN_BASE_URL") or None

    if provider == "anthropic":
        from deepeval.models.llms.anthropic_model import AnthropicModel

        return AnthropicModel(model=model_name, api_key=api_key)
    return GPTModel(model=model_name, api_key=api_key, base_url=base_url)


def precheck(env: dict[str, str]) -> tuple[LLMClient, object]:
    """预检生成 LLM + judge LLM 可用；不可达时 raise RuntimeError。"""
    if not env.get("LANGCHAIN_API_KEY"):
        raise RuntimeError("无 LANGCHAIN_API_KEY（.env.test）")
    llm = build_llm(env)
    if not llm.enabled:
        raise RuntimeError("LLM 未启用")
    llm.complete("ping", user="reply: ok")
    # deepeval metric 实例化兜底
    os.environ["OPENAI_API_KEY"] = env["LANGCHAIN_API_KEY"]
    # judge 预检
    judge_model = build_judge_model(env)
    from deepeval.test_case import LLMTestCase

    build_metrics(judge_model)["faithfulness"].measure(
        LLMTestCase(input="x", actual_output="y", retrieval_context=["z"])
    )
    return llm, judge_model


# ────────────────────────────────────────────────────────────────────────── #
# 核心 runner
# ────────────────────────────────────────────────────────────────────────── #


def run_experiment(
    profile: EvalProfile,
    *,
    env: dict[str, str] | None = None,
    output_dir: Path | None = None,
) -> dict:
    """按 ``profile`` 跑完整 RAG eval：build KB → N case × retrieve+gen+judge → 报告。

    Returns
    -------
    dict
        报告字典（同时写入 ``output_dir/<profile>_<timestamp>.json``）。
    """
    from tempfile import mkdtemp

    env = env or read_env_test()
    output_dir = output_dir or _REPORTS
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  PROFILE: {profile.name}")
    print(f"  {profile.description}")
    print(f"  chunk={profile.max_chunk_chars} embed={profile.embedding_provider}/"
          f"{profile.embedding_model or 'n/a'} rerank={profile.rerank_enabled}/"
          f"top{profile.rerank_top_n if profile.rerank_enabled else '-'} "
          f"strategy={profile.strategy} top_k={profile.top_k}")
    print(f"{'='*60}")

    llm, judge_model = precheck(env)
    corpus, goldens = load_domainrag()
    goldens = goldens[: profile.max_cases] if profile.max_cases > 0 else goldens
    print(f"  数据集: {len(corpus.docs)} docs, {len(goldens)} goldens")

    t0 = time.perf_counter()
    tmp = Path(mkdtemp(prefix=f"eval_{profile.name}_"))
    eval_kb = build_eval_kb(corpus, tmp, profile, env=env)
    print(f"  索引构建: {time.perf_counter() - t0:.1f}s")

    strategy = profile.strategy  # 已是 QueryTransformStrategy enum
    metrics = build_metrics(judge_model)

    per_case: list[dict[str, float]] = []
    case_reports: list[dict] = []
    errored: list[str] = []
    for idx, g in enumerate(goldens):
        try:
            tc, result, timing = run_rag(g, eval_kb, llm, strategy=strategy, top_k=profile.top_k)
            t0 = time.perf_counter()
            scores = evaluate_case(tc, metrics)
            timing["judge_s"] = round(time.perf_counter() - t0, 2)
        except Exception as e:  # noqa: BLE001
            errored.append(f"case#{idx} {g.question[:30]}: {e}")
            print(f"  [{idx+1}/{len(goldens)}] ❌ {e}")
            continue
        per_case.append(scores)
        case_reports.append({
            "question": g.question,
            "scores": scores,
            "degraded": result.degraded,
            "timing": timing,
            "answer": tc.actual_output[:200],
        })
        mean_score = sum(scores.values()) / len(scores) if scores else 0
        print(f"  [{idx+1}/{len(goldens)}] cv={scores['contextual_relevancy']:.2f} "
              f"ac={scores['answer_correctness']:.2f} chunks={timing['n_chunks']} "
              f"judge={timing['judge_s']:.0f}s | {g.question[:30]}")

    mean = aggregate(per_case)
    passed, detail = judge(mean)
    import statistics
    timing_summary = {}
    if case_reports:
        for key in ("retrieve_s", "generate_s", "judge_s"):
            vals = [c["timing"][key] for c in case_reports]
            timing_summary[key] = {"mean": round(statistics.mean(vals), 1),
                                   "total": round(sum(vals), 0)}

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    report = {
        "profile": profile.name,
        "description": profile.description,
        "config": asdict(profile),
        "timestamp": timestamp,
        "n": len(goldens),
        "n_success": len(per_case),
        "passed": passed,
        "mean": mean,
        "detail": detail,
        "timing": timing_summary,
        "cases": case_reports,
        "errored": errored,
    }
    out_file = output_dir / f"{profile.name}_{timestamp}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  报告: {out_file}")
    print(f"  mean: {', '.join(f'{k}={v:.3f}' for k, v in mean.items())}")
    if errored:
        print(f"  ⚠️ {len(errored)} case errored")
    return report


# ────────────────────────────────────────────────────────────────────────── #
# 报告对比
# ────────────────────────────────────────────────────────────────────────── #


def compare_reports(report_paths: list[Path]) -> None:
    """并排对比多份报告的核心指标。"""
    reports = []
    for p in report_paths:
        reports.append(json.loads(Path(p).read_text(encoding="utf-8")))

    metrics_order = ["faithfulness", "answer_relevancy", "answer_correctness",
                     "contextual_precision", "contextual_recall", "contextual_relevancy"]
    names = [r["profile"] for r in reports]
    # 表头
    hdr = f"{'metric':<24}" + "".join(f"{n:>16}" for n in names)
    print(f"\n{'='*len(hdr)}")
    print(hdr)
    print("-" * len(hdr))
    for m in metrics_order:
        vals = [r["mean"].get(m, 0) for r in reports]
        row = f"{m:<24}" + "".join(f"{v:>16.3f}" for v in vals)
        # delta vs first
        if len(vals) > 1:
            deltas = [v - vals[0] for v in vals[1:]]
            row += "  " + " ".join(
                f"{'+' if d >= 0 else ''}{d:.3f}" for d in deltas
            )
        print(row)
    print(f"\n{'n_success':<24}" + "".join(f"{r.get('n_success', '?'):>16}" for r in reports))
    if any(r.get("timing") for r in reports):
        print(f"{'judge_mean_s':<24}" + "".join(
            f"{r.get('timing', {}).get('judge_s', {}).get('mean', '?'):>16}" for r in reports))
    print(f"{'='*len(hdr)}\n")


# ────────────────────────────────────────────────────────────────────────── #
# CLI
# ────────────────────────────────────────────────────────────────────────── #


def main():
    parser = argparse.ArgumentParser(
        prog="python -m tests.rag_eval.experiment",
        description="RAG 检索优化实验框架——按 profile 跑 deepeval 裁判 + 对比报告",
    )
    parser.add_argument("profile", nargs="?", help="要跑的 profile 名（如 baseline / rerank）")
    parser.add_argument("--list", action="store_true", help="列出所有可用 profile")
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
        reports = []
        for name in names:
            profile = get_profile(name)
            if args.max_cases:
                profile.max_cases = args.max_cases
            r = run_experiment(profile)
            reports.append(r)
        # 自动对比
        print("\n\n========== SWEEP 对比 ==========")
        # 取每个 profile 的最新报告
        latest = {}
        for f in _REPORTS.glob("*.json"):
            data = json.loads(f.read_text(encoding="utf-8"))
            pn = data.get("profile", "")
            if pn in names and (pn not in latest or f.stat().st_mtime > latest[pn].stat().st_mtime):
                latest[pn] = f
        if len(latest) > 1:
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

    run_experiment(profile)


if __name__ == "__main__":
    main()
