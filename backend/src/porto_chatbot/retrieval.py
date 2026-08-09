from __future__ import annotations

from llama_index.core import QueryBundle
from llama_index.core.llms import MockLLM
from llama_index.core.retrievers import BaseRetriever, QueryFusionRetriever
from llama_index.core.retrievers.fusion_retriever import FUSION_MODES
from llama_index.core.schema import NodeWithScore, TextNode

from .bm25_index import Bm25Index
from .bm25_index import _tokenize_text as _tokenize_query
from .logging_utils import get_component_logger
from .models import SourceChunk
from .rerankers import RERANKER_BACKENDS
from .settings import Settings
from .vector_stores import VectorRepository, VectorSearchRequest

logger = get_component_logger("retrieval")


class _VectorNodeRetriever(BaseRetriever):
    """Wrap :meth:`VectorRepository.search` as a ``BaseRetriever`` for RRF fusion."""

    def __init__(
        self,
        repo: VectorRepository,
        collection: str,
        query_embedding: list[float],
        top_k: int,
    ):
        self._repo = repo
        self._collection = collection
        self._query_embedding = query_embedding
        self._top_k = top_k
        super().__init__()

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        hits = self._repo.search(
            self._collection,
            VectorSearchRequest(
                query_embedding=self._query_embedding, top_k=self._top_k,
            ),
        )
        return [
            NodeWithScore(
                node=TextNode(text=h.document, id_=h.id, metadata=dict(h.metadata)),
                score=h.score,
            )
            for h in hits
        ]


class _Bm25NodeRetriever(BaseRetriever):
    """Wrap BM25 + repository fetch as a ``BaseRetriever`` for RRF fusion.

    query needs CJK pre-tokenisation (same as corpus); hit text/metadata is
    hydrated from the repository so that node content (and thus node.hash)
    matches the vector side, allowing RRF to correctly de-duplicate.
    """

    def __init__(
        self,
        repo: VectorRepository,
        collection: str,
        bm25_index: Bm25Index | None,
        top_k: int,
    ):
        self._repo = repo
        self._collection = collection
        self._bm25 = bm25_index
        self._top_k = top_k
        super().__init__()

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        retriever = self._bm25.as_retriever(self._top_k) if self._bm25 else None
        if retriever is None:
            return []
        hits = retriever.retrieve(_tokenize_query(query_bundle.query_str))
        if not hits:
            return []
        ids = [hit.node.node_id for hit in hits]
        records = self._repo.fetch(self._collection, ids)
        record_map = {r.id: r for r in records}
        nodes: list[NodeWithScore] = []
        for hit in hits:
            cid = hit.node.node_id
            record = record_map.get(cid)
            if record is None:
                continue
            node = TextNode(
                text=record.document, id_=cid, metadata=dict(record.metadata),
            )
            nodes.append(NodeWithScore(node=node, score=float(hit.score or 0.0)))
        return nodes


def hybrid_fusion_search(
    *,
    repo: VectorRepository,
    collection: str,
    query_embedding: list[float],
    query_text: str,
    bm25_index: Bm25Index | None,
    top_k: int,
    candidate_k: int,
    vector_weight: float,
) -> list[NodeWithScore]:
    """Use llama-index ``QueryFusionRetriever`` (reciprocal_rerank/RRF) to fuse
    vector + BM25 candidates sourced entirely from the repository protocol."""
    vector_weight = min(max(vector_weight, 0.0), 1.0)
    retrievers: list[BaseRetriever] = [
        _VectorNodeRetriever(repo, collection, query_embedding, candidate_k),
        _Bm25NodeRetriever(repo, collection, bm25_index, candidate_k),
    ]
    fusion = QueryFusionRetriever(
        retrievers,
        # num_queries=1 下 llm 不会被实际调用，但 llama-index 会在构造时无条件解析 llm（不传 则回退到全局 Settings.llm，后者默认会尝试初始化 OpenAI 并因缺少 API key 报错），故显式传入 MockLLM 作占位。
        llm=MockLLM(),
        mode=FUSION_MODES.RECIPROCAL_RANK,
        similarity_top_k=top_k,
        num_queries=1,  # 关闭 LLM 生成多查询，只做候选融合（无需 LLM）
        use_async=False,
        retriever_weights=[vector_weight, 1.0 - vector_weight],
    )
    return fusion.retrieve(QueryBundle(query_str=query_text))


def rerank_chunks(chunks: list[SourceChunk], query: str, settings: Settings) -> list[SourceChunk]:
    """按 ``settings.rerank_type`` 选择 reranker 后端做二次精排。

    未启用 / 未配置 / 执行异常时均原样降级返回 ``chunks``（fail-open）。
    """
    if not settings.rerank_enabled or not chunks:
        return chunks
    backend_cls = RERANKER_BACKENDS.get(settings.rerank_type)
    if backend_cls is None:
        logger.warning("rerank skipped reason=unknown_type type=%s", settings.rerank_type)
        return chunks
    try:
        backend = backend_cls(settings)
        return backend.rerank(chunks, query)
    except Exception:
        logger.exception("rerank failed query_chars=%s candidates=%s", len(query), len(chunks))
        return chunks
