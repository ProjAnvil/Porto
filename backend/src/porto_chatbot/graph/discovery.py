"""Capability discovery: serve the best structure-graph tier for a repository.

The ladder is Tier 1 (a structure MCP server answering Cypher) → Tier 0 (the
in-process snapshot decoder) → an empty graph. Both public calls are total
functions: they never raise, and :meth:`StructureGraphRegistry.graph_for` never
returns ``None`` — a missing or broken graph is always an empty one.

Read-only by construction: this module only ever calls read tools.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from pathlib import Path

from ..logging_utils import get_component_logger
from .base import GraphNode, GraphStats, StructureGraph
from .local_graph import LocalStructureGraph
from .mcp_client import Kind, McpError, McpGraphClient, McpStructureGraph
from .snapshot import decode_snapshot

logger = get_component_logger("graph.discovery")

SNAPSHOT_NAME = "structure.vg"
SCV_GRAPH_MCP = "scv-graph-mcp"
VECTRA_MCP = "vectra-mcp"
SCV_DIST_ROOT_ENV = "SCV_PERCEPTION_DIST"

TIER_MCP = "mcp"
TIER_LOCAL = "local"
TIER_EMPTY = "empty"

# 计划里的注入点每个请求都 new 一个 registry，而一个 MCP 子进程是要活过单次
# 请求的（否则每个 retrieve 都要负担一次 handshake），所以缓存必须是模块级的。
_MCP_CLIENTS: dict[tuple[str, str, str | None], McpGraphClient] = {}
# path -> (mtime_ns, size, graph)；graph 为 None 表示「判定为不可用」，同样缓存，
# 避免每次请求都重新解码一张超限的图。
_LOCAL_GRAPHS: dict[str, tuple[int, int, LocalStructureGraph | None]] = {}

__all__ = [
    "EmptyStructureGraph",
    "StructureGraphRegistry",
    "find_scv_graph_mcp",
    "find_vectra_mcp",
    "reset_graph_caches",
    "scv_perception_dist",
]


def scv_perception_dist() -> Path:
    """Return SCV's perception dist root (mirrors SCV ``perceive.py:26``)."""
    configured = os.environ.get(SCV_DIST_ROOT_ENV)
    root = Path(configured) if configured else Path.home() / ".scv" / "perception" / "dist"
    return root.expanduser()


def find_scv_graph_mcp() -> Path | None:
    """Return SCV's multi-repo graph server when it has already been built.

    Mirrors SCV ``perceive.py:323-338`` (``build_graphmcp``): looked up only
    under the dist root, never through ``shutil.which`` — SCV itself never puts
    this binary on PATH, so a PATH search would only find it by accident. Nor
    do we ``go build`` here: compiling is SCV's installation-time job, and
    doing it at query time would pin a user request behind a toolchain nobody
    asked to run.
    """
    binary = scv_perception_dist() / "graphmcp" / SCV_GRAPH_MCP
    return binary if binary.is_file() and os.access(binary, os.X_OK) else None


def find_vectra_mcp() -> str | None:
    """Return the path of ``vectra-mcp`` on PATH (mirrors ``perceive.py:454``)."""
    return shutil.which(VECTRA_MCP)


class EmptyStructureGraph:
    """The terminal fallback of the tier ladder: a graph with nothing in it.

    Every capability call answers with an empty result, so no caller needs a
    ``None`` check. ``cypher()`` returns ``None`` — with no graph there is no
    Cypher engine either, which tells callers to skip that path entirely.
    """

    def node(self, key: str) -> GraphNode | None:
        """Return ``None``: an empty graph has no nodes."""
        return None

    def neighbors(
        self, key: str, direction: str = "out", labels: Sequence[str] | None = None
    ) -> list[GraphNode]:
        """Return no neighbors."""
        return []

    def bfs(
        self,
        keys: Sequence[str],
        max_depth: int = 2,
        labels: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[tuple[GraphNode, int]]:
        """Return no rows."""
        return []

    def cycles(self) -> list[tuple[tuple[str, ...], str]]:
        """Return no cycles."""
        return []

    def lookup(self, prop_key: str, prop_value: object, limit: int = 50) -> list[GraphNode]:
        """Return no nodes."""
        return []

    def stats(self) -> GraphStats:
        """Return zeroed counters."""
        return GraphStats()

    def cypher(self, query: str, limit: int = 200) -> list[dict] | None:
        """Return ``None``: there is no Cypher engine behind an empty graph."""
        return None


class StructureGraphRegistry:
    """Discover ``<kb_dir>/<repo>/structure.vg`` and serve the best available tier.

    Parameters
    ----------
    settings:
        Any object exposing ``graph_enabled``, ``graph_mcp_enabled``,
        ``graph_mcp_binary``, ``graph_mcp_timeout``, ``graph_max_nodes`` and
        ``kb_dirs`` — i.e. :class:`~porto_chatbot.settings.Settings`. Attributes
        are read defensively so partially-populated objects degrade instead of
        raising.
    """

    def __init__(self, settings) -> None:
        self._settings = settings
        self._empty = EmptyStructureGraph()

    def tier(self, repo: str) -> str:
        """Return which tier currently serves ``repo``.

        Returns
        -------
        str
            ``"mcp"``, ``"local"`` or ``"empty"``.
        """
        graph = self.graph_for(repo)
        if isinstance(graph, McpStructureGraph):
            return TIER_MCP
        if isinstance(graph, LocalStructureGraph):
            return TIER_LOCAL
        return TIER_EMPTY

    def graph_for(self, repo: str) -> StructureGraph:
        """Return the best graph available for ``repo``.

        Returns
        -------
        StructureGraph
            Never ``None``: an unusable repository degrades to
            :class:`EmptyStructureGraph`.
        """
        if not _is_safe_repo(repo):
            return self._empty
        if not getattr(self._settings, "graph_enabled", False):
            return self._empty
        return self._mcp_graph(repo) or self._local_graph(repo) or self._empty

    # --- Tier 1 ----------------------------------------------------------

    def _mcp_graph(self, repo: str) -> McpStructureGraph | None:
        """Return a Tier 1 graph, or ``None`` when Tier 1 is unusable."""
        if not getattr(self._settings, "graph_mcp_enabled", False):
            return None
        plan = self._serve_plan(repo)
        if plan is None:
            return None
        binary, kind, target = plan
        served = repo if kind == "scv-graph" else None
        key = (binary, target, served)
        client = _MCP_CLIENTS.get(key)
        if client is None:
            client = McpGraphClient(
                target=target,
                kind=kind,
                binary=binary,
                repo=served,
                timeout=float(getattr(self._settings, "graph_mcp_timeout", 10.0) or 10.0),
            )
            _MCP_CLIENTS[key] = client
        try:
            client.start()  # 已弃用的 client 会一直抛，后续请求直接回落 Tier 0
        except McpError as exc:
            logger.warning("structure mcp unavailable for %s: %s", repo, exc)
            return None
        return McpStructureGraph(client, served)

    def _serve_plan(self, repo: str) -> tuple[str, Kind, str] | None:
        """Pick binary, backend and target; ``None`` when Tier 1 cannot run."""
        explicit = getattr(self._settings, "graph_mcp_binary", None)
        if explicit:
            binary = str(explicit)
            kind: Kind = "scv-graph" if SCV_GRAPH_MCP in Path(binary).name else "vectra"
        else:
            installed = find_scv_graph_mcp()
            if installed is not None:  # 一个注册 Serves 所有仓库，优先
                return str(installed), "scv-graph", str(self._analysis_dir(repo))
            on_path = find_vectra_mcp()
            if on_path is None:
                return None
            binary, kind = on_path, "vectra"
        if kind == "vectra":
            snapshot = self._snapshot_path(repo)  # 单快照后端必须先有 f.vg
            if snapshot is None:
                return None
            return binary, kind, str(snapshot)
        return binary, kind, str(self._analysis_dir(repo))

    # --- Tier 0 ----------------------------------------------------------

    def _local_graph(self, repo: str) -> LocalStructureGraph | None:
        """Return a Tier 0 graph, or ``None`` when the snapshot is unusable.

        Three gates, in cost order: the mtime/size cache, the
        ``graph_max_nodes`` budget, and a catch-all that logs rather than
        raises — a broken snapshot must never surface as an error to whoever
        asked for graph context.
        """
        path = self._snapshot_path(repo)
        if path is None:
            return None
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            cached = _LOCAL_GRAPHS.get(str(path))
            if cached is not None and cached[:2] == stamp:
                return cached[2]
            data = decode_snapshot(path.read_bytes())
            budget = int(getattr(self._settings, "graph_max_nodes", 0) or 0)
            if budget and len(data.nodes) > budget:
                logger.warning(
                    "%s has %d nodes, over graph_max_nodes=%d → empty graph",
                    path,
                    len(data.nodes),
                    budget,
                )
                _LOCAL_GRAPHS[str(path)] = (*stamp, None)
                return None
            graph = LocalStructureGraph(data)
            _LOCAL_GRAPHS[str(path)] = (*stamp, graph)
            return graph
        except Exception as exc:  # noqa: BLE001 — fail-open：任何异常都降级为空图
            logger.warning("cannot load %s: %s", path, exc)
            return None

    # --- paths -----------------------------------------------------------

    def _kb_dirs(self) -> list[Path]:
        dirs = getattr(self._settings, "kb_dirs", None) or []
        return [Path(d).expanduser() for d in dirs]

    def _snapshot_path(self, repo: str) -> Path | None:
        """First ``<kb_dir>/<repo>/structure.vg`` that exists."""
        for directory in self._kb_dirs():
            candidate = directory / repo / SNAPSHOT_NAME
            if candidate.is_file():
                return candidate
        return None

    def _analysis_dir(self, repo: str) -> Path | None:
        """Knowledge-base directory serving ``repo`` (multi-repo backend input)."""
        snapshot = self._snapshot_path(repo)
        if snapshot is not None:
            return snapshot.parent.parent
        dirs = self._kb_dirs()
        return dirs[0] if dirs else None


def reset_graph_caches() -> None:
    """Drop cached graphs and close every MCP child process.

    Registry instances are created per request, so this is the only place that
    can release the module-level caches (tests, shutdown).
    """
    for client in _MCP_CLIENTS.values():
        try:
            client.close()
        except Exception:  # noqa: BLE001 — 清理阶段不因单个子进程失败而中断
            logger.debug("closing structure mcp client failed", exc_info=True)
    _MCP_CLIENTS.clear()
    _LOCAL_GRAPHS.clear()


def _is_safe_repo(repo: str) -> bool:
    """Reject repository names that would address files outside the kb dir."""
    if not repo or repo.startswith(("/", ".")):
        return False
    return not any(part in ("", ".", "..") for part in Path(repo).parts)
