"""Unit tests for the Milvus adapter using a mocked MilvusClient."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from porto_chatbot.vector_stores import (
    CollectionSpec,
    DimensionMismatchError,
    DistanceMetric,
    MilvusVectorRepository,
    VectorRecord,
    VectorRepository,
    VectorSearchRequest,
)


@pytest.fixture()
def mock_client():
    """Patch MilvusClient so we can test adapter logic without a real server."""
    with patch("porto_chatbot.vector_stores.milvus.MilvusClient") as mocked:
        client = MagicMock()
        mocked.return_value = client
        client._using = "default"
        client.prepare_index_params.return_value = MagicMock()
        yield client


@pytest.fixture()
def repo(mock_client):
    return MilvusVectorRepository(uri="http://localhost:19530")


# ── protocol conformance ──


def test_milvus_repository_satisfies_protocol(repo):
    assert isinstance(repo, VectorRepository)


# ── ensure_collection ──


def test_ensure_collection_creates_with_hnsw(repo, mock_client):
    mock_client.has_collection.return_value = False
    spec = CollectionSpec(name="kb", dimension=128, metric=DistanceMetric.L2)
    repo.ensure_collection(spec)

    mock_client.create_collection.assert_called_once()
    mock_client.load_collection.assert_called_once()


def test_ensure_collection_loads_existing(repo, mock_client):
    mock_client.has_collection.return_value = True
    spec = CollectionSpec(name="kb", dimension=128)
    repo.ensure_collection(spec)

    mock_client.load_collection.assert_called_once_with("kb")
    mock_client.create_collection.assert_not_called()


# ── upsert ──


def test_upsert_writes_records(repo, mock_client):
    repo._specs["kb"] = CollectionSpec(name="kb", dimension=3)
    records = [
        VectorRecord("a", [1.0, 0.0, 0.0], "alpha", {"chunk": 1}),
        VectorRecord("b", [0.0, 1.0, 0.0], "beta", {"chunk": 2}),
    ]
    repo.upsert("kb", records)
    mock_client.upsert.assert_called_once()
    call_data = mock_client.upsert.call_args[1]["data"]
    assert len(call_data) == 2
    assert call_data[0]["id"] == "a"


def test_upsert_dimension_mismatch(repo, mock_client):
    repo._specs["kb"] = CollectionSpec(name="kb", dimension=3)
    with pytest.raises(DimensionMismatchError):
        repo.upsert("kb", [VectorRecord("x", [1.0, 0.0], "doc", {})])


def test_upsert_empty_is_noop(repo, mock_client):
    repo.upsert("kb", [])
    mock_client.upsert.assert_not_called()


# ── search ──


def test_search_returns_hits(repo, mock_client):
    repo._specs["kb"] = CollectionSpec(name="kb", dimension=2, metric=DistanceMetric.L2)
    mock_client.search.return_value = [[
        {"id": "a", "distance": 0.5, "entity": {"document": "hello", "metadata": '{"k": 1}'}},
    ]]
    hits = repo.search("kb", VectorSearchRequest(query_embedding=[1.0, 0.0], top_k=5))
    assert len(hits) == 1
    assert hits[0].id == "a"
    assert hits[0].document == "hello"
    assert hits[0].metadata == {"k": 1}
    assert hits[0].score > 0  # exp(-0.5)


def test_search_with_where_filter(repo, mock_client):
    repo._specs["kb"] = CollectionSpec(name="kb", dimension=2)
    mock_client.search.return_value = [[]]
    repo.search(
        "kb",
        VectorSearchRequest(query_embedding=[1.0, 0.0], top_k=5, where={"session_id": "s1"}),
    )
    call_kwargs = mock_client.search.call_args[1]
    assert "filter" in call_kwargs
    assert "session_id" in call_kwargs["filter"]


def test_search_empty_results(repo, mock_client):
    repo._specs["kb"] = CollectionSpec(name="kb", dimension=2)
    mock_client.search.return_value = []
    hits = repo.search("kb", VectorSearchRequest(query_embedding=[1.0, 0.0], top_k=5))
    assert hits == []


# ── fetch ──


def test_fetch_returns_records(repo, mock_client):
    mock_client.query.return_value = [
        {"id": "a", "vector": [1.0, 0.0], "document": "hello", "metadata": '{"k": 1}'},
    ]
    records = repo.fetch("kb", ["a"])
    assert len(records) == 1
    assert records[0].id == "a"
    assert records[0].document == "hello"
    assert records[0].embedding == [1.0, 0.0]
    assert records[0].metadata == {"k": 1}


def test_fetch_empty_ids(repo, mock_client):
    assert repo.fetch("kb", []) == []
    mock_client.query.assert_not_called()


# ── count ──


def test_count(repo, mock_client):
    mock_client.get_collection_stats.return_value = {"row_count": 42}
    assert repo.count("kb") == 42


# ── collection_info ──


def test_collection_info_discovers_dimension_without_specs(repo, mock_client):
    """A fresh repo (no ensure_collection) must still report the real
    dimension from the collection schema — critical for Milvus→Milvus
    migration where the source repo has an empty _specs dict."""
    mock_client.get_collection_stats.return_value = {"row_count": 100}
    mock_client.describe_collection.return_value = {
        "fields": [
            {"name": "id", "params": {}},
            {"name": "vector", "params": {"dim": 1024}},
        ],
    }
    info = repo.collection_info("kb")
    assert info.dimension == 1024
    assert info.count == 100


# ── delete ──


def test_delete(repo, mock_client):
    repo.delete("kb", ["a", "b"])
    mock_client.delete.assert_called_once()


def test_delete_empty_is_noop(repo, mock_client):
    repo.delete("kb", [])
    mock_client.delete.assert_not_called()


# ── health ──


def test_health_ok(repo, mock_client):
    mock_client.get_server_version.return_value = "2.6.0"
    h = repo.health()
    assert h.healthy


def test_health_fail(repo, mock_client):
    mock_client.get_server_version.side_effect = Exception("unreachable")
    h = repo.health()
    assert not h.healthy


# ── score normalization ──


def test_l2_score_normalization(repo, mock_client):
    repo._specs["kb"] = CollectionSpec(name="kb", dimension=1, metric=DistanceMetric.L2)
    mock_client.search.return_value = [[
        {"id": "near", "distance": 0.0, "entity": {"document": "", "metadata": "{}"}},
        {"id": "far", "distance": 10.0, "entity": {"document": "", "metadata": "{}"}},
    ]]
    hits = repo.search("kb", VectorSearchRequest(query_embedding=[1.0], top_k=2))
    assert hits[0].score > hits[1].score  # exp(0) > exp(-10)
