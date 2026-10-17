"""Tests for the in-memory Tier 0 structure graph."""
from __future__ import annotations

from pathlib import Path

import pytest

from porto_chatbot.graph.local_graph import LocalStructureGraph
from porto_chatbot.graph.snapshot import decode_snapshot

_FIX = Path(__file__).parent / "fixtures" / "structure.vg"


@pytest.fixture()
def graph() -> LocalStructureGraph:
    return LocalStructureGraph(decode_snapshot(_FIX.read_bytes()))


def test_node_looks_up_by_key(graph):
    node = graph.node("bean:orderService")
    assert node is not None
    assert node.label == "Bean"
    assert node.props["stereotype"] == "Service"


def test_node_returns_none_for_an_unknown_key(graph):
    assert graph.node("bean:nope") is None


def test_out_neighbors_follow_injects(graph):
    keys = [n.key for n in graph.neighbors("bean:orderController")]
    assert keys == ["bean:orderService"]


def test_in_neighbors_are_sorted_by_key(graph):
    keys = [n.key for n in graph.neighbors("bean:orderService", direction="in")]
    assert keys == ["bean:orderController", "bean:payService"]


def test_both_direction_merges_in_and_out(graph):
    keys = [n.key for n in graph.neighbors("bean:orderService", direction="both")]
    assert keys == ["bean:orderController", "bean:payService"]


def test_neighbors_filters_by_label(graph):
    assert graph.neighbors("bean:orderController", labels=["CALLS"]) == []
    assert len(graph.neighbors("bean:orderController", labels=["INJECTS"])) == 1


def test_neighbors_of_an_unknown_key_is_empty(graph):
    assert graph.neighbors("bean:nope") == []


def test_neighbors_rejects_an_unknown_direction(graph):
    with pytest.raises(ValueError, match="direction"):
        graph.neighbors("bean:orderController", direction="sideways")
