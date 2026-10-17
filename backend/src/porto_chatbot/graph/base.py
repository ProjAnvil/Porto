"""Backend-neutral types and protocol for code-structure graphs.

Consumers above this layer see only these types; concrete backends (local
snapshot decoder, MCP client) are interchangeable implementations of
:class:`StructureGraph`.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "GraphEdge",
    "GraphNode",
    "GraphStats",
    "SnapshotError",
    "StructureGraph",
]


@dataclass(frozen=True)
class GraphNode:
    """A node in a structure graph.

    Attributes
    ----------
    key:
        Stable string key, e.g. ``bean:orderService`` / ``type:com.acme.T``.
    label:
        Node label: ``Bean`` / ``Type`` / ``Method`` / ``File``.
    props:
        Node properties decoded from the snapshot.
    """

    key: str
    label: str
    props: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    """A directed, labelled edge between two node keys.

    Attributes
    ----------
    src:
        Source node key.
    dst:
        Target node key.
    label:
        Edge label: ``INJECTS`` / ``CALLS`` / ``CONTAINS`` / ``IMPLEMENTS``.
    props:
        Edge properties decoded from the snapshot, e.g. ``{"via": "constructor"}``.
    """

    src: str
    dst: str
    label: str
    props: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GraphStats:
    """Size and health counters for one loaded graph.

    Attributes
    ----------
    nodes:
        Total node count.
    edges:
        Total edge count.
    index_keys:
        Property names the writer declared an index for.
    sccs:
        Number of strongly connected components, singletons included.
    cyclic_nodes:
        Nodes that sit in a component of size > 1. Self-loops alone do not
        count, matching Vectra's ``Stats.CyclicNodes``.
    """

    nodes: int = 0
    edges: int = 0
    index_keys: tuple[str, ...] = ()
    sccs: int = 0
    cyclic_nodes: int = 0


class SnapshotError(Exception):
    """Malformed or unsupported snapshot bytes.

    Callers must catch this and degrade to an empty graph.
    """


@runtime_checkable
class StructureGraph(Protocol):
    """Every structure-graph backend implements this protocol."""

    def node(self, key: str) -> GraphNode | None:
        """Return the node with ``key``, or ``None`` when absent."""

    def neighbors(
        self, key: str, direction: str = "out", labels: Sequence[str] | None = None
    ) -> list[GraphNode]:
        """Return adjacent nodes.

        Parameters
        ----------
        key:
            Anchor node key.
        direction:
            ``"out"`` / ``"in"`` / ``"both"``.
        labels:
            Optional edge-label filter; ``None`` keeps every label.

        Returns
        -------
        list[GraphNode]
            Adjacent nodes, deduplicated and ordered by key.
        """

    def bfs(
        self,
        keys: Sequence[str],
        max_depth: int = 2,
        labels: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[tuple[GraphNode, int]]:
        """Breadth-first expansion from ``keys``.

        Parameters
        ----------
        keys:
            Seed node keys; they are returned at depth 0.
        max_depth:
            Maximum hop count from a seed.
        labels:
            Edge-label filter applied to every traversal step.
        limit:
            Hard cap on returned rows.

        Returns
        -------
        list[tuple[GraphNode, int]]
            ``(node, depth)`` pairs sorted by ``(depth, key)``.
        """

    def cycles(self) -> list[tuple[tuple[str, ...], str]]:
        """Return dependency cycles and their grade.

        Returns
        -------
        list[tuple[tuple[str, ...], str]]
            One ``(members, grade)`` per cycle, grade being ``"fatal"`` or
            ``"tolerated"``.
        """

    def lookup(self, prop_key: str, prop_value: object, limit: int = 50) -> list[GraphNode]:
        """Return nodes whose ``prop_key`` equals ``prop_value``."""

    def stats(self) -> GraphStats:
        """Return size and health counters for this graph."""

    def cypher(self, query: str, limit: int = 200) -> list[dict] | None:
        """Run a read-only Cypher query.

        Returns
        -------
        list[dict] | None
            Rows, or ``None`` when Cypher is unsupported (Tier 0). ``None`` is
            a capability-negotiation signal: callers fall back to the
            protocol's built-in traversal methods.
        """
