from __future__ import annotations

from llama_index.core import QueryBundle
from llama_index.core.llms import MockLLM
from llama_index.core.retrievers import BaseRetriever, QueryFusionRetriever
from llama_index.core.retrievers.fusion_retriever import FUSION_MODES
from llama_index.core.schema import NodeWithScore, TextNode
from llama_index.core.vector_stores.types import VectorStoreQuery
from llama_index.vector_stores.chroma import ChromaVectorStore as LlamaChromaVectorStore

from .bm25_index import Bm25Index
from .bm25_index import _tokenize_text as _tokenize_query
from .logging_utils import get_component_logger
from .models import SourceChunk
from .settings import Settings

logger = get_component_logger("retrieval")


def _fetch_texts(collection, ids: list[str]) -> dict[str, tuple[str, dict]]:
    if not ids:
        return {}
    fetched = collection.get(ids=ids, include=["documents", "metadatas"])
    return {
        cid: (doc or "", dict(meta or {}))
        for cid, doc, meta in zip(
            fetched.get("ids", []),
            fetched.get("documents", []),
            fetched.get("metadatas", []),
            strict=False,
        )
    }


def vector_search(collection, query_embedding: list[float], top_k: int) -> list[NodeWithScore]:
    """向量检索：完全委托给 llama-index 的 Chroma 集成做「距离 -> 相似度」换算。

    llama-index 内部对 chroma 返回的 distance 用 ``exp(-distance)`` 转成相似度分数，
    我们不再自己手写任何 distance/score 公式。
    """
    vector_store = LlamaChromaVectorStore(chroma_collection=collection)
    result = vector_store.query(
        VectorStoreQuery(query_embedding=query_embedding, similarity_top_k=top_k)
    )
    similarities = result.similarities or [0.0] * len(result.nodes)
    return [
        NodeWithScore(node=node, score=float(score))
        for node, score in zip(result.nodes, similarities, strict=False)
    ]


class _VectorNodeRetriever(BaseRetriever):
    """把 :func:`vector_search`（llama-index Chroma 集成）包装为 BaseRetriever，供
    ``QueryFusionRetriever`` 使用。"""

    def __init__(self, collection, query_embedding: list[float], top_k: int):
        self._collection = collection
        self._query_embedding = query_embedding
        self._top_k = top_k
        super().__init__()

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        return vector_search(self._collection, self._query_embedding, self._top_k)


class _Bm25NodeRetriever(BaseRetriever):
    """把 llama-index ``BM25Retriever`` 包装为 BaseRetriever，供 ``QueryFusionRetriever`` 使用。

    query 需要用与 corpus 相同的 CJK 预分词（见 bm25_index.py）后再交给底层
    BM25Retriever；命中的文本/metadata 统一从 chroma 回填（BM25 语料内部存的是预分词后
    的乱码文本，仅供 BM25 打分使用），确保与向量侧命中同一 chunk 时 node 内容（进而
    node.hash）一致，RRF 才能正确去重合并分数。
    """

    def __init__(self, collection, bm25_index: Bm25Index | None, top_k: int):
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
        fetched = _fetch_texts(self._collection, [hit.node.node_id for hit in hits])
        nodes: list[NodeWithScore] = []
        for hit in hits:
            cid = hit.node.node_id
            text, metadata = fetched.get(cid, ("", dict(hit.node.metadata or {})))
            node = TextNode(text=text, id_=cid, metadata=metadata)
            nodes.append(NodeWithScore(node=node, score=float(hit.score or 0.0)))
        return nodes


def hybrid_fusion_search(
    *,
    collection,
    query_embedding: list[float],
    query_text: str,
    bm25_index: Bm25Index | None,
    top_k: int,
    candidate_k: int,
    vector_weight: float,
) -> list[NodeWithScore]:
    """用 llama-index ``QueryFusionRetriever``（reciprocal_rerank/RRF）融合向量 + BM25 候选。"""
    vector_weight = min(max(vector_weight, 0.0), 1.0)
    retrievers: list[BaseRetriever] = [
        _VectorNodeRetriever(collection, query_embedding, candidate_k),
        _Bm25NodeRetriever(collection, bm25_index, candidate_k),
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


def _build_rerank_llm(settings: Settings):
    """按 rerank_* 配置（缺省回退到 agent_*）构建 llama-index LLM，供 LLMRerank 使用。

    ``agent_base_url`` 非空时视为 openai-compatible 第三方端点（如 DeepSeek/Moonshot 等）；
    此时改用 ``OpenAILike`` 并显式声明 ``is_chat_model`` / ``context_window``，绕开
    ``llama-index-llms-openai`` 对官方模型名单的强校验（否则未知模型名会在访问
    ``llm.metadata`` 时抛 ``ValueError``）。
    """
    provider = settings.rerank_provider or settings.agent_provider
    model = settings.rerank_model or settings.agent_model
    api_key = settings.agent_api_key
    if not api_key:
        return None
    try:
        if provider == "openai":
            if settings.agent_base_url:
                from llama_index.llms.openai_like import OpenAILike

                return OpenAILike(
                    model=model,
                    api_key=api_key,
                    api_base=settings.agent_base_url,
                    is_chat_model=True,
                    is_function_calling_model=False,
                    context_window=max(settings.agent_max_tokens * 4, 4096),
                    temperature=0.0,
                )
            from llama_index.llms.openai import OpenAI

            return OpenAI(model=model, api_key=api_key, temperature=0.0)
        if provider == "anthropic":
            from llama_index.llms.anthropic import Anthropic

            return Anthropic(
                model=model,
                api_key=api_key,
                base_url=settings.agent_base_url or None,
            )
    except Exception:
        logger.exception("rerank llm build failed provider=%s model=%s", provider, model)
        return None
    logger.warning("rerank llm unsupported provider=%s", provider)
    return None


def rerank_chunks(chunks: list[SourceChunk], query: str, settings: Settings) -> list[SourceChunk]:
    """用 llama-index ``LLMRerank`` 对候选 chunk 做二次精排。

    未启用 / 未配置可用 LLM / 执行异常时均原样降级返回 ``chunks``（fail-open，不影响主检索链路）。
    """
    if not settings.rerank_enabled or not chunks:
        return chunks
    llm = _build_rerank_llm(settings)
    if llm is None:
        logger.info("rerank skipped reason=llm_unavailable")
        return chunks
    try:
        from llama_index.core.postprocessor import LLMRerank

        nodes = [
            NodeWithScore(
                node=TextNode(text=c.text, id_=c.id, metadata=c.metadata),
                score=c.score,
            )
            for c in chunks
        ]
        top_n = min(settings.rerank_top_n, len(nodes))
        reranker = LLMRerank(
            llm=llm,
            top_n=top_n,
            choice_batch_size=settings.rerank_choice_batch_size,
        )
        reranked = reranker.postprocess_nodes(nodes, query_bundle=QueryBundle(query_str=query))
    except Exception:
        logger.exception("rerank failed query_chars=%s candidates=%s", len(query), len(chunks))
        return chunks

    by_id = {c.id: c for c in chunks}
    result: list[SourceChunk] = []
    for node_with_score in reranked:
        original = by_id.get(node_with_score.node.id_)
        if original is None:
            continue
        result.append(
            SourceChunk(
                id=original.id,
                path=original.path,
                title=original.title,
                text=original.text,
                score=round(float(node_with_score.score or 0.0), 4),
                metadata=original.metadata,
            )
        )
    logger.info("rerank finish candidates=%s kept=%s", len(chunks), len(result))
    return result or chunks
