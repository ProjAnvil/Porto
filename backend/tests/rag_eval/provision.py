from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from porto_chatbot.settings import Settings
from porto_chatbot.vector_store import ChromaVectorStore

from .schema import RagCorpus

_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_filename(doc_id: str) -> str:
    return _SAFE.sub("_", doc_id)


@dataclass
class EvalKb:
    store: ChromaVectorStore
    settings: Settings


def build_eval_kb(
    corpus: RagCorpus, tmp_dir: Path, embedding_dimensions: int = 128
) -> EvalKb:
    """把 corpus 落成隔离 kb_dir 文件 → 隔离 Settings → build()。

    全程与用户真实 KB 零交集：data_dir/chroma_dir/kb_dirs/vector_collection 都隔离。
    embedding 固定 local（确定性、零成本）。
    """
    kb_dir = tmp_dir / "eval_kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    for doc in corpus.docs:
        (kb_dir / f"{_safe_filename(doc.id)}.md").write_text(doc.text, encoding="utf-8")

    settings = Settings(
        kb_dirs=[kb_dir],
        data_dir=tmp_dir / "eval_data",
        log_dir=tmp_dir / "eval_logs",
        embedding_provider="local",
        embedding_dimensions=embedding_dimensions,
        vector_collection="eval_domainrag",
        retrieval_method="hybrid",
        rerank_enabled=False,
    )
    store = ChromaVectorStore(settings)
    store.build(reset=True)
    return EvalKb(store=store, settings=settings)
