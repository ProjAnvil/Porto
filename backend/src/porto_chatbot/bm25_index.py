from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from llama_index.core.schema import TextNode
from llama_index.retrievers.bm25 import BM25Retriever

from .embeddings import tokens
from .logging_utils import get_component_logger
from .settings import Settings

logger = get_component_logger("bm25_index")

# llama-index BM25Retriever 默认按英文单词边界正则分词（`\b\w\w+\b`），对无空格的中文
# 整段文本会把连续汉字当成一个 token，检索效果很差。这里复用 embedding 侧的 CJK 分词
# 逻辑（单字 + 双字 bigram）预先切好词、空格拼接，再用 `\S+` 的 token_pattern 让
# BM25Retriever 按空白切分 —— 分词交给我们自己，BM25 算法本身完全交给 llama-index。
_TOKEN_PATTERN = r"\S+"


@dataclass
class ChunkRecord:
    """与 chroma chunk 一一对应的 BM25 语料条目。"""

    chroma_id: str
    text: str
    metadata: dict


def _tokenize_text(text: str) -> str:
    """复用 embedding 侧分词（CJK 单字+双字 bigram），拼空格串喂 llama-index BM25Retriever。"""
    return " ".join(tokens(text))


class Bm25Index:
    """基于 ``llama_index.retrievers.bm25.BM25Retriever`` 的稀疏检索索引封装。

    对外仍保留 build/query/save/load 的简单接口，供 vector_store.py 与 retrieval.py
    复用；BM25 算法本身、打分、持久化均委托给 llama-index，不再直接依赖 bm25s。
    """

    def __init__(self) -> None:
        self._retriever: BM25Retriever | None = None
        self._size = 0

    def build(self, chunks: list[ChunkRecord]) -> None:
        nodes = [
            TextNode(text=_tokenize_text(c.text), id_=c.chroma_id, metadata=c.metadata)
            for c in chunks
        ]
        self._retriever = BM25Retriever.from_defaults(
            nodes=nodes,
            similarity_top_k=max(len(nodes), 1),
            token_pattern=_TOKEN_PATTERN,
            skip_stemming=True,
        )
        self._size = len(nodes)
        logger.info("bm25 index built chunks=%s", len(nodes))

    def as_retriever(self, top_k: int) -> BM25Retriever | None:
        """返回底层 llama-index ``BM25Retriever``（供 hybrid RRF 融合直接使用），并设置 top_k。

        调用方需自行用 :func:`_tokenize_text` 预分词 query 后再传给
        ``retriever.retrieve()``，因为 BM25Retriever 的 query 侧分词与 corpus 侧一致
        （同样按空白切分预分词好的 token 串）。
        """
        if self._retriever is None or self._size == 0:
            return None
        self._retriever.similarity_top_k = min(max(top_k, 1), self._size)
        return self._retriever

    def query(self, text: str, top_k: int) -> list[tuple[str, dict, float]]:
        """返回 [(chroma_id, metadata, score)]，按 bm25 分数降序。"""
        retriever = self.as_retriever(top_k)
        if retriever is None:
            return []
        nodes_with_scores = retriever.retrieve(_tokenize_text(text))
        return [
            (n.node.node_id, dict(n.node.metadata or {}), float(n.score or 0.0))
            for n in nodes_with_scores
        ]

    def __len__(self) -> int:
        return self._size

    def save(self, dir: Path) -> None:
        if self._retriever is None:
            raise RuntimeError("cannot save an unbuilt index")
        dir.mkdir(parents=True, exist_ok=True)
        self._retriever.persist(str(dir))
        logger.info("bm25 index saved dir=%s chunks=%s", dir, self._size)

    @classmethod
    def load(cls, dir: Path) -> Bm25Index:
        idx = cls()
        retriever = BM25Retriever.from_persist_dir(str(dir))
        # llama-index 当前版本 persist/from_persist_dir 只保存 similarity_top_k /
        # verbose / corpus_weight_mask，不保存 token_pattern / skip_stemming，reload
        # 后会悄悄退回默认英文分词正则，导致 query 侧分词与 build 时不一致（中文检索
        # 静默失效）。这里手动复原，确保重启/reload 后行为与刚 build 完一致。
        retriever.token_pattern = _TOKEN_PATTERN
        retriever.skip_stemming = True
        idx._retriever = retriever
        idx._size = int(retriever.bm25.scores.get("num_docs", 0) or 0)
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

