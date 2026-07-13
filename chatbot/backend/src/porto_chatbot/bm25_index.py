from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import bm25s

from .embeddings import tokens
from .logging_utils import get_component_logger
from .settings import Settings

logger = get_component_logger("bm25_index")


@dataclass
class ChunkRecord:
    """与 chroma chunk 一一对应的 BM25 语料条目。"""

    chroma_id: str
    text: str
    metadata: dict


def _tokenize_text(text: str) -> str:
    """复用 embedding 侧分词（CJK 单字+双字 bigram），拼空格串喂 bm25s。"""
    return " ".join(tokens(text))


class Bm25Index:
    """基于 bm25s 的稀疏检索索引。

    build 时按 chunks 顺序建 BM25；retrieve 返回文档位置索引，靠 self._ids /
    self._metadatas 映射回 chroma chunk id + metadata。save/load 用 bm25s 原生
    落盘 + sidecar(ids/metadatas) json。
    """

    def __init__(self) -> None:
        self._retriever: bm25s.BM25 | None = None
        self._ids: list[str] = []
        self._metadatas: list[dict] = []

    def build(self, chunks: list[ChunkRecord]) -> None:
        self._ids = [c.chroma_id for c in chunks]
        self._metadatas = [c.metadata for c in chunks]
        corpus = [_tokenize_text(c.text) for c in chunks]
        corpus_tokens = bm25s.tokenize(corpus, stemmer=None, stopwords=None, show_progress=False)
        self._retriever = bm25s.BM25(method="lucene", k1=1.5, b=0.75)
        self._retriever.index(corpus_tokens, show_progress=False)
        logger.info("bm25 index built chunks=%s", len(chunks))

    def query(self, text: str, top_k: int) -> list[tuple[str, dict, float]]:
        """返回 [(chroma_id, metadata, score)]，按 bm25 分数降序。"""
        if self._retriever is None or not self._ids:
            return []
        q_tokens = bm25s.tokenize([_tokenize_text(text)], stemmer=None, stopwords=None, show_progress=False)
        results, scores = self._retriever.retrieve(q_tokens, k=min(top_k, len(self._ids)), show_progress=False)
        out: list[tuple[str, dict, float]] = []
        for idx, score in zip(results[0], scores[0], strict=False):
            i = int(idx)
            if 0 <= i < len(self._ids):
                out.append((self._ids[i], self._metadatas[i], float(score)))
        return out

    def __len__(self) -> int:
        return len(self._ids)

    def save(self, dir: Path) -> None:
        if self._retriever is None:
            raise RuntimeError("cannot save an unbuilt index")
        dir.mkdir(parents=True, exist_ok=True)
        self._retriever.save(str(dir))  # 不传 corpus：只落 index arrays
        (dir / "ids.json").write_text(json.dumps(self._ids, ensure_ascii=False), encoding="utf-8")
        (dir / "metadatas.json").write_text(
            json.dumps(self._metadatas, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("bm25 index saved dir=%s chunks=%s", dir, len(self._ids))

    @classmethod
    def load(cls, dir: Path) -> "Bm25Index":
        idx = cls()
        idx._retriever = bm25s.BM25.load(str(dir), load_corpus=False, mmap=True)
        idx._ids = json.loads((dir / "ids.json").read_text(encoding="utf-8"))
        idx._metadatas = json.loads((dir / "metadatas.json").read_text(encoding="utf-8"))
        return idx


class Bm25Registry:
    """进程级 Bm25Index 缓存（按 data_dir）。"""

    _cache: dict[str, Bm25Index] = {}

    @staticmethod
    def _dir(settings: Settings) -> Path:
        return settings.data_dir / "bm25s_index"

    @classmethod
    def get(cls, settings: Settings) -> Bm25Index | None:
        key = str(settings.data_dir)
        if key in cls._cache:
            return cls._cache[key]
        d = cls._dir(settings)
        if not d.exists():
            return None
        try:
            idx = Bm25Index.load(d)
        except Exception:
            logger.exception("bm25 load failed dir=%s", d)
            return None
        cls._cache[key] = idx
        return idx

    @classmethod
    def build_and_save(cls, settings: Settings, chunks: list[ChunkRecord]) -> Bm25Index:
        idx = Bm25Index()
        idx.build(chunks)
        idx.save(cls._dir(settings))
        cls._cache[str(settings.data_dir)] = idx
        return idx

    @classmethod
    def invalidate(cls, data_dir: Path) -> None:
        cls._cache.pop(str(data_dir), None)
