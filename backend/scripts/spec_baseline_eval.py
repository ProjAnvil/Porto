#!/usr/bin/env python
"""spec 质量基线对照：模板生成（地板） vs LLM evaluator-optimizer loop（2026-08 重建）。

与旧版的区别（旧版调用已删除的 ``PortoAgent.run()``，随 Task 9 架构演进失效）：
- 走**当前生产编排**：直接驱动 ``build_workflow_graph()``（与 WorkflowExecutor 相同的
  graph/节点/中断拓扑，仅省去 store/HTTP 层），全程 LangGraph。
- 固定 PRD golden 集（``tests/workflow_eval/prds/*.md``）——回归锚点。
- **独立 judge**：基线与 loop 的最终 spec 都由独立 critic LLM
  （``PORTO_CHATBOT_CRITIC_*``，缺省回退 generator 并警示）重新打分，消除
  「loop 自己的 critic 给自己迭代产物打分」的分数膨胀。
- 同时报告 loop 内分数 vs 独立分数的差（inflation 估计）。

跑法：
  cd backend
  uv run python scripts/spec_baseline_eval.py               # 全部 PRD
  uv run python scripts/spec_baseline_eval.py --max-prds 1  # 只跑第一份
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import time
import uuid
from pathlib import Path

from porto_chatbot.agent import PortoAgent
from porto_chatbot.agent.graph import build_workflow_graph
from porto_chatbot.agent.heuristics import infer_project_name
from porto_chatbot.llm import LLMClient
from porto_chatbot.settings import Settings
from porto_chatbot.specs import SpecContext, critique_spec
from porto_chatbot.vector_store import ChromaVectorStore

_BACKEND = Path(__file__).resolve().parent.parent
_PRD_DIR = _BACKEND / "tests" / "workflow_eval" / "prds"
_REPORT_DIR = _BACKEND / "tests" / "workflow_eval" / "reports"


def _load_env() -> None:
    """加载 backend/.env.test（若有）再叠 .env；已有环境变量优先。"""
    for name in (".env.test", ".env"):
        f = _BACKEND / name
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _tmp_saver(tmp_dir: Path):
    conn = sqlite3.connect(str(tmp_dir / "g.sqlite3"), check_same_thread=False)
    from langgraph.checkpoint.sqlite import SqliteSaver

    sv = SqliteSaver(conn)
    sv.setup()
    return sv


def _eval_settings(tmp_dir: Path, *, refine: bool) -> Settings:
    """隔离的 eval Settings：独立 data/log/kb 目录 + 唯一 collection。"""
    kb = tmp_dir / "kb"
    kb.mkdir(parents=True, exist_ok=True)
    return Settings(
        kb_dirs=[kb],
        data_dir=tmp_dir / "data",
        log_dir=tmp_dir / "logs",
        vector_collection=f"speceval_{uuid.uuid4().hex[:8]}",
        spec_refine_enabled=refine,
    )


def _run_graph(prd_text: str, settings: Settings, tmp_dir: Path) -> dict:
    """驱动生产 graph 跑到 END（retrieve→understand→identify→generate 子图 fan-out）。

    与 ``WorkflowExecutor._run_start/_run_advance`` 同构：invoke 到首个 interrupt，
    再两次 ``stream(None)`` 依次过 identify / generate。
    """
    agent = PortoAgent(settings, ChromaVectorStore(settings), LLMClient(settings))
    graph = build_workflow_graph(_tmp_saver(tmp_dir))
    wid = f"speceval_{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": wid, "agent": agent}}
    initial = {
        "workflow_id": wid,
        "project_name": infer_project_name(prd_text),
        "prd_file_id": prd_text,  # text 路径：file_service=None 时节点回退内联文本
        "top_k": settings.top_k,
        "steps": [],
        "sources": [],
        "understanding": "",
        "subsystems": [],
        "specs": {},
        "spec_results": {},
    }
    graph.invoke(initial, config)      # retrieve → understand，停
    list(graph.stream(None, config))   # identify，停
    list(graph.stream(None, config))   # generate（Send fan-out）→ END
    return dict(graph.get_state(config).values)


def _independent_critique(
    judge_llm: LLMClient, prd_text: str, understanding: str, subsystems, specs
) -> list[dict]:
    """独立 judge 按生产 rubric 给最终 spec 重新打分（不走 loop 内 critic）。"""
    ctx = SpecContext(llm=judge_llm, state={"prd_text": prd_text, "understanding": understanding})
    out = []
    for sub in subsystems:
        spec = specs.get(sub.name, "")
        critique = critique_spec(ctx, sub, spec) if spec else None
        out.append({
            "subsystem": sub.name,
            "score": critique.score if critique else 0,
            "verdict": critique.verdict if critique else "N/A",
            "per_dimension": critique.per_dimension if critique else {},
        })
    return out


def _inloop_scores(final_state: dict) -> dict[str, float | None]:
    """从 spec_results 的 attempts 提取各子系统 loop 内最高分（分数膨胀对照用）。"""
    out: dict[str, float | None] = {}
    for name, r in (final_state.get("spec_results") or {}).items():
        attempts = getattr(r, "attempts", None) or (r.get("attempts") if isinstance(r, dict) else None)
        scores = [a.score for a in attempts or [] if getattr(a, "score", None) is not None]
        out[name] = max(scores) if scores else None
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="spec 质量基线对照（模板 vs LLM loop，独立 judge）")
    parser.add_argument("--prds", type=Path, default=_PRD_DIR, help="PRD golden 集目录")
    parser.add_argument("--max-prds", type=int, default=0, help="只跑前 N 份 PRD（0=全部）")
    parser.add_argument("--report-dir", type=Path, default=_REPORT_DIR)
    args = parser.parse_args()

    _load_env()
    base_settings = Settings()
    if not base_settings.agent_api_key:
        print("❌ 未检测到 LLM 配置（LANGCHAIN_API_KEY）。请在 backend/.env 或 .env.test 配置。")
        raise SystemExit(1)

    # 独立 judge：优先 PORTO_CHATBOT_CRITIC_*（与生产 spec loop 的 critic 同源配置）
    independent = bool(base_settings.critic_provider)
    judge_settings = Settings()
    if independent:
        judge_settings.agent_provider = base_settings.critic_provider
        judge_settings.agent_api_key = base_settings.critic_api_key or base_settings.agent_api_key
        judge_settings.agent_base_url = base_settings.critic_base_url or base_settings.agent_base_url
        judge_settings.agent_model = base_settings.critic_model or base_settings.agent_model
        judge_settings.agent_temperature = base_settings.critic_temperature
    judge_llm = LLMClient(judge_settings)
    judge_desc = (
        f"{judge_settings.agent_model}（独立 critic）"
        if independent
        else f"{judge_settings.agent_model}（⚠️ 与 generator 同模型，非独立）"
    )
    print(f"✅ generator={base_settings.agent_model} judge={judge_desc}")

    prds = sorted(args.prds.glob("*.md"))
    if args.max_prds:
        prds = prds[: args.max_prds]
    if not prds:
        print(f"❌ 未找到 PRD（{args.prds}）")
        raise SystemExit(1)
    print(f"PRD 集: {[p.name for p in prds]}\n")

    from tempfile import mkdtemp

    results = []
    for prd_file in prds:
        prd_text = prd_file.read_text(encoding="utf-8")
        print(f"\n{'='*60}\n📄 {prd_file.name}\n{'='*60}")

        t0 = time.time()
        tmp_a = Path(mkdtemp(prefix="speceval_off_"))
        st_off = _eval_settings(tmp_a, refine=False)
        state_off = _run_graph(prd_text, st_off, tmp_a)
        baseline = _independent_critique(
            judge_llm, prd_text, state_off.get("understanding", ""),
            state_off.get("subsystems", []), state_off.get("specs", {}),
        )
        t_base = time.time() - t0

        t0 = time.time()
        tmp_b = Path(mkdtemp(prefix="speceval_on_"))
        st_on = _eval_settings(tmp_b, refine=True)
        state_on = _run_graph(prd_text, st_on, tmp_b)
        loop = _independent_critique(
            judge_llm, prd_text, state_on.get("understanding", ""),
            state_on.get("subsystems", []), state_on.get("specs", {}),
        )
        inloop = _inloop_scores(state_on)
        t_loop = time.time() - t0

        b_avg = statistics.mean([c["score"] for c in baseline]) if baseline else 0.0
        l_avg = statistics.mean([c["score"] for c in loop]) if loop else 0.0
        il_vals = [v for v in inloop.values() if v is not None]
        il_avg = statistics.mean(il_vals) if il_vals else None
        print(f"  模板基线: {len(baseline)} 子系统, 独立 judge 均分 {b_avg:.2f}/12 ({t_base:.0f}s)")
        print(f"  LLM loop: {len(loop)} 子系统, 独立 judge 均分 {l_avg:.2f}/12 ({t_loop:.0f}s)")
        if il_avg is not None:
            print(f"  loop 内分数均分 {il_avg:.2f}/12（与独立 judge 差 {il_avg - l_avg:+.2f} = 膨胀估计）")
        results.append({
            "prd": prd_file.name,
            "n_subsystems": len(loop),
            "baseline": {"avg": round(b_avg, 2), "per_subsystem": baseline},
            "loop": {"avg": round(l_avg, 2), "per_subsystem": loop},
            "loop_inloop_avg": round(il_avg, 2) if il_avg is not None else None,
            "duration_s": {"baseline": round(t_base), "loop": round(t_loop)},
        })

    b_all = statistics.mean([r["baseline"]["avg"] for r in results])
    l_all = statistics.mean([r["loop"]["avg"] for r in results])
    print("\n" + "=" * 60)
    print(f"总体（{len(results)} PRD，独立 judge）:")
    print(f"  模板基线均分 : {b_all:.2f} / 12")
    print(f"  LLM loop 均分 : {l_all:.2f} / 12")
    delta = l_all - b_all
    print(f"  delta       : {delta:+.2f}  {'✅ loop 显著优于模板' if delta > 0.5 else '⚠️ 提升有限'}")
    print("=" * 60)

    args.report_dir.mkdir(parents=True, exist_ok=True)
    out = args.report_dir / f"spec_baseline_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({
        "timestamp": time.strftime("%Y%m%d_%H%M%S"),
        "judge": {"model": judge_settings.agent_model, "independent": independent},
        "generator": base_settings.agent_model,
        "overall": {"baseline_avg": round(b_all, 2), "loop_avg": round(l_all, 2)},
        "prds": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {out}")


if __name__ == "__main__":
    main()
