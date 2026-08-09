#!/usr/bin/env python3
"""临时恢复脚本：从 Chroma 复制数据到当前 active Milvus profile。

根因：迁移 6ec41d6a (源/目标同为同一 Milvus 实例、同 collection 名)
      drop_collection 时把源数据一起删了 → porto_kb/memory 全空。

本脚本直接读 Chroma 原始数据 → drop+recreate Milvus collection(按实际维度)
→ 批量 upsert → flush → 计数验证。

用法:
    cd backend && .venv/bin/python scripts/recover_chroma_to_milvus.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# ── path setup ──
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR / "src"))

from porto_chatbot.settings import Settings  # noqa: E402
from porto_chatbot.vector_profiles import VectorProfileStore  # noqa: E402
from porto_chatbot.vector_stores import (  # noqa: E402
    ChromaVectorRepository,
    CollectionSpec,
    DistanceMetric,
    MilvusVectorRepository,
)

CHROMA_PATH = Path.home() / ".porto-chatbot" / "chroma"
BATCH_SIZE = 500
COLLECTIONS = [
    ("porto_kb", "kb"),
    ("porto_memory", "memory"),
]


def main() -> int:
    settings = Settings()
    profile_store = VectorProfileStore(settings)
    profile_store.bootstrap()
    active = profile_store.active()
    if active is None:
        print("FATAL: no active profile")
        return 1
    if active.backend != "milvus":
        print(f"FATAL: active profile is {active.backend}, not milvus")
        return 1

    print(f"Active profile: {active.name} ({active.id})")
    print(f"  backend={active.backend}  uri={active.uri}")
    print(f"  metric={active.metric}")
    print()

    source = ChromaVectorRepository(CHROMA_PATH)
    target = MilvusVectorRepository.from_profile(active)

    for collection_name, kind in COLLECTIONS:
        print(f"── {collection_name} ({kind}) ──")

        # 1. Detect source dimension
        src_info = source.collection_info(collection_name)
        dim = src_info.dimension
        src_count = source.count(collection_name)
        print(f"  source: count={src_count}, dim={dim}")
        if src_count == 0:
            print("  SKIP (empty in source)")
            continue
        if dim is None:
            print("  FATAL: cannot detect dimension")
            return 1

        # 2. Drop + recreate target with correct dimension
        metric = (
            DistanceMetric.COSINE if active.metric == "cosine" else DistanceMetric.L2
        )
        target.drop_collection(collection_name)
        # Wait for Milvus GC to settle after drop to avoid race condition
        # where the new collection gets caught in the old collection's GC sweep.
        print("  waiting 3s for Milvus GC to settle after drop...")
        time.sleep(3)
        target.ensure_collection(
            CollectionSpec(
                name=collection_name,
                dimension=dim,
                metric=metric,
                metadata={"kind": kind},
            )
        )
        print(f"  target: recreated dim={dim} metric={metric.value}")

        # 3. Batch copy
        copied = 0
        t0 = time.time()
        for batch in source.export_batches(collection_name, BATCH_SIZE):
            target.upsert(collection_name, batch)
            copied += len(batch)
            elapsed = time.time() - t0
            rate = copied / elapsed if elapsed > 0 else 0
            print(f"  copied {copied}/{src_count}  ({rate:.0f} rec/s)")

        # 4. Flush + verify
        target.flush(collection_name)
        tgt_count = target.count(collection_name)
        ok = tgt_count == src_count
        status = "✅ OK" if ok else "❌ MISMATCH"
        print(f"  verify: source={src_count} target={tgt_count}  {status}")
        print()

    # 5. Health check
    health = target.health()
    print(f"Milvus health: healthy={health.healthy}  detail={health.detail}")

    source.close()
    target.close()

    print("\nDone. Restart the Porto backend to pick up the restored data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
