"""Tests for the in-memory Tier 0 structure graph."""
from __future__ import annotations

from pathlib import Path

import pytest

from porto_chatbot.graph.base import GraphEdge, GraphNode
from porto_chatbot.graph.local_graph import LocalStructureGraph
from porto_chatbot.graph.snapshot import SnapshotData, decode_snapshot

_FIX = Path(__file__).parent / "fixtures" / "structure.vg"


def _make(nodes, edges) -> LocalStructureGraph:
    """Build a graph from ``(key, label)`` / ``(src, dst, label, props)`` tuples."""
    return LocalStructureGraph(
        SnapshotData(
            nodes=[GraphNode(key=k, label=label, props={}) for k, label in nodes],
            edges=[
                GraphEdge(src=s, dst=d, label=label, props=props)
                for s, d, label, props in edges
            ],
            index_keys=[],
        )
    )


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


# ── bfs ──


def test_bfs_reaches_pay_service_at_depth_two(graph):
    rows = [(n.key, d) for n, d in graph.bfs(["bean:orderController"], max_depth=2)]
    assert rows == [("bean:orderService", 1), ("bean:payService", 2)]


def test_bfs_excludes_the_seeds_themselves(graph):
    keys = [n.key for n, _ in graph.bfs(["bean:orderController"])]
    assert "bean:orderController" not in keys


def test_bfs_respects_max_depth(graph):
    keys = [n.key for n, _ in graph.bfs(["bean:orderController"], max_depth=1)]
    assert keys == ["bean:orderService"]


def test_bfs_defaults_to_injects_only(graph):
    # 默认 label 是 INJECTS：否则会从 Type 经 CONTAINS 扩散到全仓 Method。
    assert graph.bfs(["type:com.acme.OrderService"]) == []
    assert len(graph.bfs(["type:com.acme.OrderService"], labels=["CONTAINS"])) == 1


def test_bfs_filters_by_label(graph):
    assert graph.bfs(["bean:orderController"], labels=["CALLS"]) == []


def test_bfs_limit_is_a_hard_cap(graph):
    assert len(graph.bfs(["bean:orderController"], limit=1)) == 1


def test_bfs_returns_pairs_sorted_by_depth_then_key(graph):
    # 同层内按 key 排序：插入顺序是 B→A，结果必须是 A→B。
    synthetic = _make(
        [("x", "Bean"), ("b", "Bean"), ("a", "Bean")],
        [("x", "b", "INJECTS", {}), ("x", "a", "INJECTS", {})],
    )
    rows = [(n.key, d) for n, d in synthetic.bfs(["x"])]
    assert rows == [("a", 1), ("b", 1)]


def test_bfs_of_an_unknown_seed_is_empty(graph):
    assert graph.bfs(["bean:nope"]) == []


# ── cycles ──


def test_fixture_bean_cycle_is_graded_fatal(graph):
    cycles = graph.cycles()
    assert len(cycles) == 1
    (members, grade) = cycles[0]
    assert set(members) == {"bean:orderService", "bean:payService"}
    assert grade == "fatal"


def test_non_injects_cycle_is_not_reported():
    # SCV 语义：CALLS 互递归不算用户可见的循环依赖，整环跳过。
    g = _make(
        [("a", "Method"), ("b", "Method")],
        [("a", "b", "CALLS", {}), ("b", "a", "CALLS", {})],
    )
    assert g.cycles() == []


def test_injects_cycle_without_fatal_via_is_tolerated():
    g = _make(
        [("a", "Bean"), ("b", "Bean")],
        [
            ("a", "b", "INJECTS", {"via": "field"}),
            ("b", "a", "INJECTS", {"via": "setter"}),
        ],
    )
    assert g.cycles() == [(("a", "b"), "tolerated")]


@pytest.mark.parametrize("via", ["constructor", "lombok-constructor", "beanMethod"])
def test_fatal_via_grades_the_cycle_fatal(via):
    g = _make(
        [("a", "Bean"), ("b", "Bean")],
        [("a", "b", "INJECTS", {"via": via}), ("b", "a", "INJECTS", {})],
    )
    assert g.cycles() == [(("a", "b"), "fatal")]


def test_mixed_cycle_is_graded_on_its_injects_edge_only():
    g = _make(
        [("a", "Bean"), ("b", "Bean")],
        [("a", "b", "INJECTS", {}), ("b", "a", "CALLS", {})],
    )
    assert g.cycles() == [(("a", "b"), "tolerated")]


def test_self_loop_is_not_a_cycle():
    g = _make([("a", "Bean")], [("a", "a", "INJECTS", {"via": "constructor"})])
    assert g.cycles() == []


def test_deep_chain_does_not_exhaust_the_recursion_limit():
    # Tarjan 必须是迭代的：单链 5000 远超 CPython 递归上限。
    depth = 5000
    g = _make(
        [(f"n{i}", "Bean") for i in range(depth)],
        [(f"n{i}", f"n{i + 1}", "INJECTS", {}) for i in range(depth - 1)],
    )
    assert g.cycles() == []
