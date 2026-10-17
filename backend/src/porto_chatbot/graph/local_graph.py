"""In-memory Tier 0 structure graph built from a decoded Vectra snapshot.

Tier 0 answers every query from RAM: no Cypher, no subprocess, no extra
dependency. ``cypher()`` therefore returns ``None`` so callers can detect the
missing capability and fall back to the protocol's traversal methods.
"""
from __future__ import annotations

from collections.abc import Sequence

from .base import GraphEdge, GraphNode
from .snapshot import SnapshotData

_DIRECTIONS = ("out", "in", "both")
# 默认只走 INJECTS：双面加载下不过滤 label 会从 Bean 经 CONTAINS 扩散到全仓 Method。
_DEFAULT_LABELS: tuple[str, ...] = ("INJECTS",)


class LocalStructureGraph:
    """Read-only structure graph backed by adjacency lists.

    Parameters
    ----------
    data:
        A decoded snapshot. Nodes are indexed by key; edges feed both the
        outgoing and the incoming adjacency lists.
    """

    def __init__(self, data: SnapshotData) -> None:
        self._nodes: dict[str, GraphNode] = {n.key: n for n in data.nodes}
        self._edges: tuple[GraphEdge, ...] = tuple(data.edges)
        self._out: dict[str, list[tuple[str, str]]] = {}
        self._in: dict[str, list[tuple[str, str]]] = {}
        for edge in self._edges:
            # 值为 (对方 key, 边 label)：props 走 edge 对象，邻接表只存遍历需要的字段。
            self._out.setdefault(edge.src, []).append((edge.dst, edge.label))
            self._in.setdefault(edge.dst, []).append((edge.src, edge.label))

    def node(self, key: str) -> GraphNode | None:
        """Return the node with ``key``, or ``None`` when absent."""
        return self._nodes.get(key)

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
        if direction not in _DIRECTIONS:
            raise ValueError(f"unknown direction {direction!r}, expected one of {_DIRECTIONS}")
        if key not in self._nodes:
            return []
        pairs: list[tuple[str, str]] = []
        if direction in ("out", "both"):
            pairs.extend(self._out.get(key, ()))
        if direction in ("in", "both"):
            pairs.extend(self._in.get(key, ()))
        wanted = None if labels is None else set(labels)
        keys = {k for k, label in pairs if wanted is None or label in wanted}
        return [self._nodes[k] for k in sorted(keys) if k in self._nodes]

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
            Seed node keys. Seeds are expanded but never returned themselves,
            so the result is the impact surface around them.
        max_depth:
            Maximum hop count from a seed.
        labels:
            Edge-label filter applied to every step; defaults to
            ``("INJECTS",)``.
        limit:
            Hard cap on returned rows.

        Returns
        -------
        list[tuple[GraphNode, int]]
            ``(node, depth)`` pairs ordered by ``(depth, key)``, depth starting
            at 1.
        """
        wanted = set(_DEFAULT_LABELS if labels is None else labels)
        # 起点先入 visited：它们是锚点而非影响面，depth 0 不出现在结果里。
        visited = {k for k in keys if k in self._nodes}
        frontier = sorted(visited)
        rows: list[tuple[GraphNode, int]] = []
        for depth in range(1, max_depth + 1):
            nxt: list[str] = []
            for key in frontier:
                for other, label in self._out.get(key, ()):
                    if label in wanted and other in self._nodes and other not in visited:
                        visited.add(other)
                        nxt.append(other)
            if not nxt:
                break
            nxt.sort()  # 同层按 key 排序，逐层拼接即 (depth, key) 全局有序
            rows.extend((self._nodes[k], depth) for k in nxt)
            if len(rows) >= limit:
                break
            frontier = nxt
        return rows[:limit]

    def cypher(self, query: str, limit: int = 200) -> list[dict] | None:
        """Return ``None``: Tier 0 has no Cypher engine."""
        return None
