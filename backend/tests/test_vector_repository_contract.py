"""Contract tests for the backend-neutral VectorRepository protocol.

These tests exercise the Chroma adapter but assert behaviour required by the
protocol — any future backend must satisfy the same suite.
"""
from __future__ import annotations

import pytest

from porto_chatbot.vector_stores import (
    ChromaVectorRepository,
    CollectionSpec,
    DimensionMismatchError,
    DistanceMetric,
    VectorRecord,
    VectorSearchRequest,
)

# ── round-trip & ordering ──


@pytest.mark.parametrize("metric", [DistanceMetric.L2, DistanceMetric.COSINE])
def test_chroma_contract_round_trips_and_orders_records(tmp_path, metric):
    repo = ChromaVectorRepository(tmp_path / "chroma")
    spec = CollectionSpec(
        name="records", dimension=3, metric=metric, metadata={"kind": "kb"},
    )
    repo.ensure_collection(spec)
    repo.upsert("records", [
        VectorRecord("b", [0.0, 1.0, 0.0], "beta", {"chunk": 2, "source_mtime": 1.5}),
        VectorRecord("a", [1.0, 0.0, 0.0], "alpha", {"chunk": 1, "source_mtime": 1.0}),
    ])
    assert [
        row.id for batch in repo.export_batches("records", 1) for row in batch
    ] == ["a", "b"]
    assert repo.fetch("records", ["a"])[0].metadata == {"chunk": 1, "source_mtime": 1.0}


# ── idempotent upsert ──


def test_upsert_is_idempotent(tmp_path):
    repo = ChromaVectorRepository(tmp_path / "chroma")
    repo.ensure_collection(CollectionSpec(name="col", dimension=2))
    rec = VectorRecord("x", [1.0, 0.0], "hello", {"k": 1})
    repo.upsert("col", [rec])
    repo.upsert("col", [rec])
    assert repo.count("col") == 1
    fetched = repo.fetch("col", ["x"])
    assert len(fetched) == 1
    assert fetched[0].document == "hello"


def test_upsert_replaces_existing_document(tmp_path):
    repo = ChromaVectorRepository(tmp_path / "chroma")
    repo.ensure_collection(CollectionSpec(name="col", dimension=2))
    repo.upsert("col", [VectorRecord("x", [1.0, 0.0], "old", {"v": 1})])
    repo.upsert("col", [VectorRecord("x", [1.0, 0.0], "new", {"v": 2})])
    fetched = repo.fetch("col", ["x"])
    assert fetched[0].document == "new"
    assert fetched[0].metadata == {"v": 2}


# ── search with where filters ──


def test_search_with_where_filter(tmp_path):
    repo = ChromaVectorRepository(tmp_path / "chroma")
    repo.ensure_collection(CollectionSpec(name="memory", dimension=2))
    repo.upsert("memory", [
        VectorRecord("m1", [1.0, 0.0], "hello", {"session_id": "s1"}),
        VectorRecord("m2", [0.9, 0.1], "world", {"session_id": "s2"}),
    ])
    hits = repo.search(
        "memory",
        VectorSearchRequest(
            query_embedding=[1.0, 0.0], top_k=5, where={"session_id": "s1"},
        ),
    )
    assert len(hits) == 1
    assert hits[0].id == "m1"


def test_search_returns_empty_for_empty_collection(tmp_path):
    repo = ChromaVectorRepository(tmp_path / "chroma")
    repo.ensure_collection(CollectionSpec(name="empty_col", dimension=2))
    hits = repo.search(
        "empty_col",
        VectorSearchRequest(query_embedding=[1.0, 0.0], top_k=5),
    )
    assert hits == []


def test_search_orders_by_relevance(tmp_path):
    """Closer vector gets a higher score."""
    repo = ChromaVectorRepository(tmp_path / "chroma")
    repo.ensure_collection(
        CollectionSpec(name="ranking", dimension=1, metric=DistanceMetric.L2),
    )
    repo.upsert("ranking", [
        VectorRecord("near", [1.0], "near", {}),
        VectorRecord("far", [100.0], "far", {}),
    ])
    hits = repo.search(
        "ranking", VectorSearchRequest(query_embedding=[1.0], top_k=2),
    )
    assert hits[0].id == "near"
    assert hits[0].score > hits[1].score


# ── count & delete ──


def test_count_and_delete(tmp_path):
    repo = ChromaVectorRepository(tmp_path / "chroma")
    repo.ensure_collection(CollectionSpec(name="col", dimension=2))
    repo.upsert("col", [
        VectorRecord("a", [1.0, 0.0], "a", {}),
        VectorRecord("b", [0.0, 1.0], "b", {}),
    ])
    assert repo.count("col") == 2
    repo.delete("col", ["a"])
    assert repo.count("col") == 1
    assert repo.fetch("col", ["a"]) == []


# ── stable export (snapshot semantics) ──


def test_export_batches_snapshot_is_stable(tmp_path):
    """A record inserted after the first batch must not appear in traversal."""
    repo = ChromaVectorRepository(tmp_path / "chroma")
    repo.ensure_collection(CollectionSpec(name="col", dimension=2))
    repo.upsert("col", [
        VectorRecord("a", [1.0, 0.0], "alpha", {}),
        VectorRecord("b", [0.0, 1.0], "beta", {}),
    ])
    gen = repo.export_batches("col", 1)
    first_batch = next(gen)
    assert [r.id for r in first_batch] == ["a"]
    # Insert a third record mid-traversal.
    repo.upsert("col", [VectorRecord("z_new", [0.5, 0.5], "gamma", {})])
    remaining = [row.id for batch in gen for row in batch]
    assert remaining == ["b"]
    assert repo.count("col") == 3  # the insert did land


def test_export_batches_empty_collection(tmp_path):
    repo = ChromaVectorRepository(tmp_path / "chroma")
    repo.ensure_collection(CollectionSpec(name="col", dimension=2))
    batches = list(repo.export_batches("col", 10))
    assert batches == []


# ── metadata type preservation ──


def test_metadata_types_preserved(tmp_path):
    repo = ChromaVectorRepository(tmp_path / "chroma")
    repo.ensure_collection(CollectionSpec(name="col", dimension=2))
    repo.upsert("col", [
        VectorRecord(
            "x", [1.0, 0.0], "doc",
            {"int_val": 42, "float_val": 3.14, "str_val": "hello", "bool_val": True},
        ),
    ])
    fetched = repo.fetch("col", ["x"])
    md = fetched[0].metadata
    assert md["int_val"] == 42
    assert isinstance(md["int_val"], int)
    assert md["float_val"] == pytest.approx(3.14)
    assert md["str_val"] == "hello"
    assert md["bool_val"] is True


# ── dimension mismatch ──


def test_dimension_mismatch_raises_without_deleting(tmp_path):
    repo = ChromaVectorRepository(tmp_path / "chroma")
    repo.ensure_collection(CollectionSpec(name="col", dimension=3))
    # Correct insert works.
    repo.upsert("col", [VectorRecord("good", [1.0, 0.0, 0.0], "ok", {})])
    # Wrong dimension must raise.
    with pytest.raises(DimensionMismatchError):
        repo.upsert("col", [VectorRecord("bad", [1.0, 0.0], "bad", {})])
    # Collection is untouched.
    assert repo.count("col") == 1
    assert repo.fetch("col", ["good"])[0].document == "ok"


def test_dimension_mismatch_on_search(tmp_path):
    repo = ChromaVectorRepository(tmp_path / "chroma")
    repo.ensure_collection(CollectionSpec(name="col", dimension=3))
    repo.upsert("col", [VectorRecord("a", [1.0, 0.0, 0.0], "a", {})])
    with pytest.raises(DimensionMismatchError):
        repo.search("col", VectorSearchRequest(query_embedding=[1.0, 0.0], top_k=1))


# ── collection info ──


def test_collection_info(tmp_path):
    repo = ChromaVectorRepository(tmp_path / "chroma")
    repo.ensure_collection(
        CollectionSpec(name="col", dimension=4, metric=DistanceMetric.COSINE),
    )
    repo.upsert("col", [
        VectorRecord("a", [1.0, 0.0, 0.0, 0.0], "a", {}),
        VectorRecord("b", [0.0, 1.0, 0.0, 0.0], "b", {}),
    ])
    info = repo.collection_info("col")
    assert info.name == "col"
    assert info.dimension == 4
    assert info.count == 2
    assert info.metric == DistanceMetric.COSINE


# ── protocol conformance ──


def test_chroma_repository_satisfies_protocol(tmp_path):
    from porto_chatbot.vector_stores import VectorRepository

    repo = ChromaVectorRepository(tmp_path / "chroma")
    assert isinstance(repo, VectorRepository)
