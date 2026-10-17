"""Contract tests for the backend-neutral structure-graph types."""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from porto_chatbot.graph import (
    GraphEdge,
    GraphNode,
    GraphStats,
    SnapshotError,
    StructureGraph,
)


def test_node_carries_key_label_and_default_props():
    node = GraphNode(key="bean:orderService", label="Bean")
    assert node.props == {}


def test_edge_is_a_labelled_direction():
    edge = GraphEdge(src="a", dst="b", label="INJECTS")
    assert (edge.src, edge.dst, edge.label) == ("a", "b", "INJECTS")
    assert edge.props == {}


def test_nodes_are_frozen_and_props_not_shared():
    a, b = GraphNode("a", "Bean"), GraphNode("b", "Bean")
    assert a.props is not b.props
    with pytest.raises(FrozenInstanceError):
        a.key = "c"  # type: ignore[misc]


def test_stats_defaults_to_an_empty_graph():
    stats = GraphStats()
    assert (stats.nodes, stats.edges, stats.sccs, stats.cyclic_nodes) == (0, 0, 0, 0)
    assert stats.index_keys == ()


def test_snapshot_error_is_catchable_as_exception():
    assert issubclass(SnapshotError, Exception)


def test_protocol_matches_a_minimal_implementation():
    class _Impl:
        def node(self, key): ...
        def neighbors(self, key, direction="out", labels=None): ...
        def bfs(self, keys, max_depth=2, labels=None, limit=50): ...
        def cycles(self): ...
        def lookup(self, prop_key, prop_value, limit=50): ...
        def stats(self): ...
        def cypher(self, query, limit=200): ...

    assert isinstance(_Impl(), StructureGraph)


def test_protocol_rejects_a_partial_implementation():
    class _Partial:
        def node(self, key): ...

    assert not isinstance(_Partial(), StructureGraph)
