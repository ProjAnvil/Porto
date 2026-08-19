#!/usr/bin/env python
"""汇总四优化手段 × 七场景矩阵：从 reports/ 取每个 (profile, dataset) 的最新报告。"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

REPORTS = Path(__file__).resolve().parents[1] / "tests" / "rag_eval" / "reports"

LEVERS = [
    ("baseline", "基线(对照)"),
    ("semantic_embed", "①语义embedding"),
    ("condense", "②condense门控"),
    ("bigchunk_rerank", "③rerank"),
    ("bigchunk_multi_query", "④multi_query"),
]
SUBSETS = ["extractive", "noisy", "faithful", "multidoc", "structured", "time_sensitive", "conversation"]


def latest_reports() -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], tuple[float, dict]] = {}
    for f in glob.glob(str(REPORTS / "*.json")):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        key = (d.get("profile", ""), d.get("dataset", ""))
        if key[0] not in {p for p, _ in LEVERS} or not key[1].startswith("domainrag"):
            continue
        mtime = Path(f).stat().st_mtime
        if key not in out or mtime > out[key][0]:
            out[key] = (mtime, d)
    return {k: v[1] for k, v in out.items()}


def main() -> None:
    reports = latest_reports()
    # ── recall 矩阵 ──
    print("\n════════ recall@k 矩阵 ════════")
    hdr = f"{'场景':<16}" + "".join(f"{label:>14}" for _, label in LEVERS)
    print(hdr)
    print("-" * len(hdr))
    for sub in SUBSETS:
        row = f"{sub:<16}"
        for prof, _ in LEVERS:
            r = reports.get((prof, f"domainrag:{sub}"))
            v = (r or {}).get("retrieval", {}).get("mean", {}).get("recall_at_k")
            row += f"{v:>14.3f}" if v is not None else f"{'—':>14}"
        print(row)
    # ── MRR 矩阵 ──
    print("\n════════ MRR 矩阵 ════════")
    print(hdr)
    print("-" * len(hdr))
    for sub in SUBSETS:
        row = f"{sub:<16}"
        for prof, _ in LEVERS:
            r = reports.get((prof, f"domainrag:{sub}"))
            v = (r or {}).get("retrieval", {}).get("mean", {}).get("mrr")
            row += f"{v:>14.3f}" if v is not None else f"{'—':>14}"
        print(row)
    # ── hit 矩阵 ──
    print("\n════════ hit@k 矩阵 ════════")
    print(hdr)
    print("-" * len(hdr))
    for sub in SUBSETS:
        row = f"{sub:<16}"
        for prof, _ in LEVERS:
            r = reports.get((prof, f"domainrag:{sub}"))
            v = (r or {}).get("retrieval", {}).get("mean", {}).get("hit_at_k")
            row += f"{v:>14.3f}" if v is not None else f"{'—':>14}"
        print(row)
    # ── 噪音率矩阵 ──
    print("\n════════ noise_ratio 矩阵（越低越好）════════")
    print(hdr)
    print("-" * len(hdr))
    for sub in SUBSETS:
        row = f"{sub:<16}"
        for prof, _ in LEVERS:
            r = reports.get((prof, f"domainrag:{sub}"))
            v = (r or {}).get("retrieval", {}).get("mean", {}).get("noise_ratio")
            row += f"{v:>14.3f}" if v is not None else f"{'—':>14}"
        print(row)
    # ── 场景均值 + 相对基线 delta ──
    print("\n════════ 各 lever 全场景均值与对基线提升 ════════")
    for prof, label in LEVERS:
        vals, d_recall = [], []
        for sub in SUBSETS:
            r = reports.get((prof, f"domainrag:{sub}"))
            b = reports.get(("baseline", f"domainrag:{sub}"))
            rm = (r or {}).get("retrieval", {}).get("mean", {})
            bm = (b or {}).get("retrieval", {}).get("mean", {})
            if rm.get("recall_at_k") is not None:
                vals.append(rm["recall_at_k"])
                if bm.get("recall_at_k") is not None:
                    d_recall.append(rm["recall_at_k"] - bm["recall_at_k"])
        avg = sum(vals) / len(vals) if vals else 0
        avg_d = sum(d_recall) / len(d_recall) if d_recall else 0
        print(f"{label:<18} recall 均值={avg:.3f}  Δrecall(vs 基线)={avg_d:+.3f}")
    # ── condense 门控命中率（conversation）──
    r = reports.get(("condense", "domainrag:conversation"))
    if r:
        n_applied = sum(1 for c in r["cases"] if c.get("condense_applied"))
        print(f"\ncondense 门控（conversation）：{n_applied}/{len(r['cases'])} 轮触发改写")
    n_missing = [(p, s) for p, _ in LEVERS for s in SUBSETS
                 if (p, f"domainrag:{s}") not in reports]
    if n_missing:
        print(f"\n⚠️ 缺失 {len(n_missing)} 组：{n_missing}", file=sys.stderr)


if __name__ == "__main__":
    main()
