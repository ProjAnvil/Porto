"""Reranker 后端：Strategy Pattern + Registry。

从 retrieval.py 提取 LLM rerank 逻辑，新增 cross-encoder reranker，
通过 ``RERANKER_BACKENDS`` 注册表按 ``rerank_type`` dispatch。
"""
from __future__ import annotations

import httpx
from typing import Protocol

from llama_index.core import QueryBundle
from llama_index.core.schema import NodeWithScore, TextNode

from .logging_utils import get_component_logger
from .models import SourceChunk
from .models.enums import LLMProvider, RerankType
from .settings import Settings

logger = get_component_logger("rerankers")


def _build_rerank_llm(settings: Settings):
    """按 rerank_* 配置（缺省回退到 agent_*）构建 llama-index LLM。

    从 retrieval.py 原样提取，零行为变化。
    """
    provider = settings.rerank_provider or settings.agent_provider
    model = settings.rerank_model or settings.agent_model
    api_key = settings.agent_api_key
    if not api_key:
        return None
    try:
        if provider == LLMProvider.OPENAI:
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
        if provider == LLMProvider.ANTHROPIC:
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


class RerankerBackend(Protocol):
    """Reranker 后端接口。"""

    def rerank(self, chunks: list[SourceChunk], query: str) -> list[SourceChunk]: ...


class LLMReranker:
    """LLM 提示重排（llama-index LLMRerank）。

    从 retrieval.py 的 rerank_chunks() + _build_rerank_llm() 提取。
    fail-open：LLM 不可用时原样返回 chunks。
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._llm = _build_rerank_llm(settings)
        self._top_n = settings.rerank_top_n
        self._batch_size = settings.rerank_choice_batch_size

    def rerank(self, chunks: list[SourceChunk], query: str) -> list[SourceChunk]:
        if self._llm is None:
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
            top_n = min(self._top_n, len(nodes))
            reranker = LLMRerank(
                llm=self._llm,
                top_n=top_n,
                choice_batch_size=self._batch_size,
            )
            reranked = reranker.postprocess_nodes(
                nodes, query_bundle=QueryBundle(query_str=query)
            )
        except Exception:
            logger.exception(
                "rerank failed query_chars=%s candidates=%s", len(query), len(chunks)
            )
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


class CrossEncoderReranker:
    """专用 cross-encoder reranker（POST {base_url}/rerank）。

    兼容 Jina / Cohere / Voyage 的 /v1/rerank 协议。
    fail-open：任何异常返回原始 chunks。
    """

    def __init__(self, settings: Settings):
        self._base_url = settings.rerank_base_url
        self._api_key = settings.rerank_api_key
        self._model = settings.rerank_model
        self._top_n = settings.rerank_top_n
        self._timeout = settings.agent_request_timeout

    def rerank(self, chunks: list[SourceChunk], query: str) -> list[SourceChunk]:
        if not self._base_url or not self._api_key:
            logger.info("cross-encoder rerank skipped reason=missing_config")
            return chunks
        try:
            top_n = min(self._top_n, len(chunks))
            # Voyage API 要求 documents 为 [str]，Jina/Cohere 要求 [{text}]。
            # 按 base_url host 判断发送格式。
            is_voyage = "voyageai.com" in (self._base_url or "")
            documents = [c.text for c in chunks] if is_voyage else [{"text": c.text} for c in chunks]
            resp = httpx.post(
                f"{self._base_url}/rerank",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "query": query,
                    "documents": documents,
                    "top_n": top_n,
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or data.get("data") or []
            if not results:
                logger.warning("cross-encoder rerank empty results")
                return chunks
            by_index = {i: c for i, c in enumerate(chunks)}
            reranked: list[SourceChunk] = []
            for item in results:
                idx = item.get("index")
                score = float(item.get("relevance_score", 0.0))
                original = by_index.get(idx)
                if original is None:
                    continue
                reranked.append(
                    SourceChunk(
                        id=original.id,
                        path=original.path,
                        title=original.title,
                        text=original.text,
                        score=round(score, 4),
                        metadata=original.metadata,
                    )
                )
            logger.info(
                "cross-encoder rerank finish candidates=%s kept=%s",
                len(chunks),
                len(reranked),
            )
            return reranked or chunks
        except Exception:
            logger.exception(
                "cross-encoder rerank failed query_chars=%s candidates=%s",
                len(query),
                len(chunks),
            )
            return chunks


RERANKER_BACKENDS: dict[RerankType, type[RerankerBackend]] = {
    RerankType.LLM: LLMReranker,
    RerankType.CROSS_ENCODER: CrossEncoderReranker,
}
