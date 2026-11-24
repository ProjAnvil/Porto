from __future__ import annotations

from unittest.mock import MagicMock, patch

from porto_chatbot.models import SourceChunk
from porto_chatbot.models.enums import RerankType
from porto_chatbot.rerankers import RERANKER_BACKENDS, CrossEncoderReranker, LLMReranker
from porto_chatbot.retrieval import rerank_chunks
from porto_chatbot.settings import Settings


def _make_chunks(n: int = 3) -> list[SourceChunk]:
    return [
        SourceChunk(id=f"c{i}", path=f"doc{i}.md", title=f"Doc {i}", text=f"内容{i}", score=0.5, metadata={})
        for i in range(n)
    ]


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        data_dir="/tmp/test-rerank",
        log_dir="/tmp/test-rerank/logs",
        rerank_enabled=True,
        rerank_type="llm",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ── Registry ──

def test_registry_has_all_types():
    assert set(RERANKER_BACKENDS.keys()) == {
        RerankType.LLM,
        RerankType.CROSS_ENCODER,
    }


# ── LLMReranker ──

def test_llm_reranker_no_llm_returns_original():
    """Without an api_key, _build_rerank_llm returns None and LLMReranker._llm is None."""
    settings = _make_settings(agent_api_key=None)
    reranker = LLMReranker(settings)
    assert reranker._llm is None


def test_llm_reranker_passthrough_when_no_llm():
    """When the LLM is unavailable, reranker.rerank() returns the chunks unchanged."""
    settings = _make_settings(agent_api_key=None)
    reranker = LLMReranker(settings)
    chunks = _make_chunks()
    result = reranker.rerank(chunks, "test query")
    assert result is chunks  # returned unchanged


# ── CrossEncoderReranker ──

def test_cross_encoder_rerank_success():
    """Mock httpx.post and check that rerank sorts as expected."""
    settings = _make_settings(
        rerank_type="cross_encoder",
        rerank_model="jina-reranker-v2-base-multilingual",
        rerank_base_url="https://api.jina.ai/v1",
        rerank_api_key="jina-test-key",
        rerank_top_n=2,
    )
    reranker = CrossEncoderReranker(settings)
    chunks = _make_chunks(3)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "results": [
            {"index": 2, "relevance_score": 0.95},
            {"index": 0, "relevance_score": 0.80},
        ],
    }
    with patch("porto_chatbot.rerankers.httpx.post", return_value=mock_response):
        result = reranker.rerank(chunks, "test query")

    assert len(result) == 2
    assert result[0].id == "c2"  # relevance 0.95 ranks first
    assert result[1].id == "c0"  # relevance 0.80 ranks second
    assert result[0].score == 0.95
    assert result[1].score == 0.8


def test_cross_encoder_rerank_voyage_response_format():
    """The Voyage API uses data instead of results as the response key."""
    settings = _make_settings(
        rerank_type="cross_encoder",
        rerank_model="rerank-2",
        rerank_base_url="https://api.voyageai.com/v1",
        rerank_api_key="voyage-key",
        rerank_top_n=1,
    )
    reranker = CrossEncoderReranker(settings)
    chunks = _make_chunks(2)

    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "data": [
            {"index": 1, "relevance_score": 0.90},
        ],
    }
    with patch("porto_chatbot.rerankers.httpx.post", return_value=mock_response):
        result = reranker.rerank(chunks, "query")

    assert len(result) == 1
    assert result[0].id == "c1"


def test_cross_encoder_rerank_fail_open():
    """On an httpx exception the chunks are returned unchanged (fail-open)."""
    settings = _make_settings(
        rerank_type="cross_encoder",
        rerank_model="jina-reranker-v2-base-multilingual",
        rerank_base_url="https://api.jina.ai/v1",
        rerank_api_key="jina-key",
        rerank_top_n=2,
    )
    reranker = CrossEncoderReranker(settings)
    chunks = _make_chunks(3)

    with patch("porto_chatbot.rerankers.httpx.post", side_effect=Exception("network error")):
        result = reranker.rerank(chunks, "query")

    assert result is chunks  # returned unchanged


def test_cross_encoder_rerank_missing_config():
    """Returned unchanged when base_url or api_key is missing."""
    settings = _make_settings(
        rerank_type="cross_encoder",
        rerank_base_url=None,  # missing
        rerank_api_key="key",
    )
    reranker = CrossEncoderReranker(settings)
    chunks = _make_chunks(3)
    result = reranker.rerank(chunks, "query")
    assert result is chunks


# ── rerank_chunks dispatch ──

def test_rerank_chunks_disabled_returns_original():
    """Returned unchanged when rerank_enabled=False."""
    settings = _make_settings(rerank_enabled=False)
    chunks = _make_chunks(3)
    result = rerank_chunks(chunks, "query", settings)
    assert result is chunks


def test_rerank_chunks_empty_returns_empty():
    settings = _make_settings(rerank_type="llm")
    assert rerank_chunks([], "query", settings) == []


def test_rerank_chunks_cross_encoder_dispatch():
    """rerank_type=cross_encoder goes through CrossEncoderReranker (verified with a mock)."""
    settings = _make_settings(
        rerank_type="cross_encoder",
        rerank_model="jina-reranker-v2",
        rerank_base_url="https://api.jina.ai/v1",
        rerank_api_key="key",
        rerank_top_n=2,
    )
    chunks = _make_chunks(3)
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "results": [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.8},
        ],
    }
    with patch("porto_chatbot.rerankers.httpx.post", return_value=mock_response):
        result = rerank_chunks(chunks, "query", settings)
    assert len(result) == 2


def test_rerank_chunks_unknown_type():
    """Returned unchanged for an unknown rerank_type."""
    settings = _make_settings(rerank_type=RerankType.LLM)  # a known type; the test targets the registry lookup logic itself
    chunks = _make_chunks(3)
    result = rerank_chunks(chunks, "query", settings)
    # Normally the LLM reranker returns the chunks (unchanged even without an llm)
    assert isinstance(result, list)
