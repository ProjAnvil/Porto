"""In-memory Tier 0 structure graph built from a decoded Vectra snapshot.

Tier 0 answers every query from RAM: no Cypher, no subprocess, no extra
dependency. ``cypher()`` therefore returns ``None`` so callers can detect the
missing capability and fall back to the protocol's traversal methods.
"""
from __future__ import annotations

from collections.abc import Sequence

from .base import GraphEdge, GraphNode, GraphStats, ident_tokens
from .snapshot import SnapshotData

_DIRECTIONS = ("out", "in", "both")
# 默认只走 INJECTS：双面加载下不过滤 label 会从 Bean 经 CONTAINS 扩散到全仓 Method。
_DEFAULT_LABELS: tuple[str, ...] = ("INJECTS",)
# 与 SCV perception/loader 的 fatalVia 一致：构造器注入在环里 Spring 无法满足。
_FATAL_VIA = frozenset({"constructor", "lombok-constructor", "beanMethod"})


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
        self._index_keys: tuple[str, ...] = tuple(data.index_keys)
        self._out: dict[str, list[GraphEdge]] = {}
        self._in: dict[str, list[GraphEdge]] = {}
        for edge in self._edges:
            self._out.setdefault(edge.src, []).append(edge)
            self._in.setdefault(edge.dst, []).append(edge)
        self._scc_cache: list[list[str]] | None = None

    def node(self, key: str) -> GraphNode | None:
        """Return the node with ``key``, or ``None`` when absent."""
        return self._nodes.get(key)

    def find(self, text: str, limit: int = 20) -> list[GraphNode]:
        """Return nodes whose key mentions ``text``.

        Tier 0 has no index to lean on, so this is a scan over the keys; the
        graph is already in memory and the result is capped early, which makes
        the scan cheaper than maintaining an inverted index for it.
        """
        tokens = ident_tokens(text)
        if not tokens:
            return []
        hits: list[GraphNode] = []
        for key in sorted(self._nodes, key=lambda k: (len(k), k)):
            lowered = key.lower()
            if any(token in lowered for token in tokens):
                hits.append(self._nodes[key])
                if len(hits) >= limit:
                    break
        return hits

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
        adjacent: list[GraphEdge] = []
        if direction in ("out", "both"):
            adjacent.extend(self._out.get(key, ()))
        if direction in ("in", "both"):
            adjacent.extend(self._in.get(key, ()))
        wanted = None if labels is None else set(labels)
        keys = set()
        for edge in adjacent:
            if wanted is not None and edge.label not in wanted:
                continue
            # 同一对节点可能有多条不同 label 的边，按对方 key 去重。
            keys.add(edge.dst if edge.src == key else edge.src)
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
                for edge in self._out.get(key, ()):
                    other = edge.dst
                    if edge.label in wanted and other in self._nodes and other not in visited:
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

    def _sccs(self) -> list[list[str]]:
        """Return strongly connected components (Tarjan, iterative).

        Iterative on purpose: a repository graph easily exceeds CPython's
        recursion limit. The result is cached — ``cycles()`` and ``stats()``
        both need it.
        """
        if self._scc_cache is None:
            index_of: dict[str, int] = {}
            low: dict[str, int] = {}
            on_stack: set[str] = set()
            pending: list[str] = []
            components: list[list[str]] = []
            counter = 0
            for root in self._nodes:
                if root in index_of:
                    continue
                index_of[root] = low[root] = counter
                counter += 1
                pending.append(root)
                on_stack.add(root)
                work = [(root, iter(self._out.get(root, ())))]
                while work:
                    node, successors = work[-1]
                    for edge in successors:
                        other = edge.dst
                        if other not in self._nodes:
                            continue
                        if other not in index_of:
                            index_of[other] = low[other] = counter
                            counter += 1
                            pending.append(other)
                            on_stack.add(other)
                            work.append((other, iter(self._out.get(other, ()))))
                            break
                        if other in on_stack:
                            low[node] = min(low[node], index_of[other])
                    else:
                        work.pop()
                        if work:
                            parent = work[-1][0]
                            low[parent] = min(low[parent], low[node])
                        if low[node] == index_of[node]:
                            component: list[str] = []
                            while True:
                                member = pending.pop()
                                on_stack.discard(member)
                                component.append(member)
                                if member == node:
                                    break
                            components.append(component)
            self._scc_cache = components
        return self._scc_cache

    def cycles(self) -> list[tuple[tuple[str, ...], str]]:
        """Return dependency cycles graded the way SCV grades them.

        Only components of size >= 2 count; within a component only ``INJECTS``
        edges are inspected, and a component without any is skipped entirely
        (mutual ``CALLS`` recursion is not a user-facing dependency cycle).
        A component is ``"fatal"`` when any of its inner INJECTS edges carries
        ``via`` in {``constructor``, ``lombok-constructor``, ``beanMethod``}.

        Returns
        -------
        list[tuple[tuple[str, ...], str]]
            ``(members, grade)`` per cycle, members sorted for determinism.
        """
        found: list[tuple[tuple[str, ...], str]] = []
        for component in self._sccs():
            if len(component) < 2:
                continue
            members = frozenset(component)
            injects = 0
            fatal = False
            for key in members:
                for edge in self._out.get(key, ()):
                    if edge.label != "INJECTS" or edge.dst not in members:
                        continue
                    injects += 1
                    if edge.props.get("via") in _FATAL_VIA:
                        fatal = True
            if injects == 0:
                continue
            found.append((tuple(sorted(members)), "fatal" if fatal else "tolerated"))
        found.sort()
        return found

    def lookup(self, prop_key: str, prop_value: object, limit: int = 50) -> list[GraphNode]:
        """Return nodes whose ``prop_key`` equals ``prop_value``.

        The graph is already in memory, so a linear scan beats maintaining a
        secondary index that most repositories would never pay back.

        Parameters
        ----------
        prop_key:
            Property name, e.g. ``"stereotype"``.
        prop_value:
            Value to match exactly.
        limit:
            Hard cap on returned rows.

        Returns
        -------
        list[GraphNode]
            Matching nodes in snapshot order.
        """
        hits: list[GraphNode] = []
        for node in self._nodes.values():
            if node.props.get(prop_key) == prop_value:
                hits.append(node)
                if len(hits) >= limit:
                    break
        return hits

    def stats(self) -> GraphStats:
        """Return size and health counters for this graph.

        Returns
        -------
        GraphStats
            ``sccs`` counts every component (singletons included) and
            ``cyclic_nodes`` only the members of components larger than one,
            matching Vectra's ``Stats`` so Tier 0 and Tier 1 agree.
        """
        components = self._sccs()
        return GraphStats(
            nodes=len(self._nodes),
            edges=len(self._edges),
            index_keys=tuple(self._index_keys),
            sccs=len(components),
            cyclic_nodes=sum(len(c) for c in components if len(c) > 1),
        )

    def cypher(self, query: str, limit: int = 200) -> list[dict] | None:
        """Return ``None``: Tier 0 has no Cypher engine."""
        return None
