"""Tier 1 structure graphs served by a structure MCP child process.

Both servers speak newline-delimited JSON-RPC over stdio and expose the same
read-only tool names with **different arguments**, so the backend identity is
carried by ``kind``::

    scv-graph-mcp -analysis-dir <dir>   cypher(repo, query, limit)  get_node(repo, key)
    vectra-mcp    -snapshot <file.vg>   cypher(query, limit)        get_node(key)

Everything here is fail-open: a malformed reply, a tool error or a hung child
turns into :class:`McpError` (or an empty result one layer up), never into an
exception reaching the caller.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..logging_utils import get_component_logger
from .base import GraphNode, GraphStats, ident_tokens

logger = get_component_logger("graph.mcp")

PROTOCOL_VERSION = "2025-06-18"
Kind = Literal["scv-graph", "vectra"]
KINDS: tuple[str, ...] = ("scv-graph", "vectra")
_DEFAULT_BINARY: dict[str, str] = {"scv-graph": "scv-graph-mcp", "vectra": "vectra-mcp"}

__all__ = ["KINDS", "Kind", "McpError", "McpGraphClient", "McpResult", "McpStructureGraph", "mcp_argv"]


class McpError(RuntimeError):
    """The MCP server could not answer a request; the caller must degrade."""


@dataclass(frozen=True)
class McpResult:
    """One tool answer: its raw payload plus a Cypher-shaped projection.

    Attributes
    ----------
    payload:
        ``structuredContent`` as returned by the server. Row-oriented tools
        (``cypher``) fill ``columns``/``rows``; node-oriented ones (``stats``,
        ``get_node``, ``cycles``) carry their own shape here.
    columns:
        Column names of a row-oriented answer, in server order.
    rows:
        Rows of a row-oriented answer; every cell is already a plain JSON value
        (nodes and edges arrive as ``{key,label,props}`` maps).
    count:
        Rows returned by this call.
    total:
        True result size before truncation — bigger than ``count`` means the
        rows were cut.
    """

    payload: dict[str, Any] = field(default_factory=dict)
    columns: tuple[str, ...] = ()
    rows: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0
    total: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> McpResult:
        """Wrap raw ``structuredContent``, projecting row-oriented answers."""
        rows = payload.get("rows")
        return cls(
            payload=payload,
            columns=tuple(payload.get("columns") or ()),
            rows=[dict(row) for row in rows] if isinstance(rows, Iterable) else [],
            count=int(payload.get("count") or 0),
            total=int(payload.get("total") or 0),
        )


def mcp_argv(binary: str, kind: str, target: str | Path) -> list[str]:
    """Return the command line for one structure MCP server.

    Parameters
    ----------
    binary:
        Path or name of the server executable.
    kind:
        ``"scv-graph"`` for the multi-repo SCV server, ``"vectra"`` for the
        per-snapshot Vectra server.
    target:
        Analysis directory (``scv-graph``) or ``structure.vg`` path
        (``vectra``).

    Returns
    -------
    list[str]
        argv ready for :class:`subprocess.Popen`.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown mcp backend {kind!r}, expected one of {KINDS}")
    flag = "-analysis-dir" if kind == "scv-graph" else "-snapshot"
    return [binary, flag, str(target)]


class _LineReader:
    """Background pump turning the child's stdout into a thread-safe queue.

    Why a thread and not plain blocking reads: a single ``tools/call`` must be
    able to give up when the server stops answering (a Cypher scan over a huge
    graph, a crashed server holding the pipe open), and threading is the only
    deadline-aware option that stays inside the standard library.
    """

    def __init__(self, stream) -> None:
        self._lines: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._pump, args=(stream,), name="porto-mcp-stdout", daemon=True
        )
        self._thread.start()

    def _pump(self, stream) -> None:
        try:
            for line in stream:
                self._lines.put(line)
        except Exception:  # pipe closed / decode error → 一律按 EOF 处理
            pass
        finally:
            self._lines.put(None)

    def get(self, timeout: float) -> str | None:
        """Return the next line, or ``None`` at EOF.

        Raises
        ------
        TimeoutError
            No complete line arrived within ``timeout`` seconds.
        """
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"no line within {timeout}s") from exc


class McpGraphClient:
    """Minimal read-only MCP client owning one stdio child process.

    Parameters
    ----------
    kind:
        ``"scv-graph"`` or ``"vectra"``; decides argv and tool arguments.
    target:
        Analysis directory or snapshot file passed to the server.
    binary:
        Executable to run; defaults to the well-known name of ``kind``.
    repo:
        Repository this client serves. Only the multi-repo backend forwards it
        with every request.
    timeout:
        Per-request deadline in seconds.

    Raises
    ------
    McpError
        On any transport failure. Failed clients are marked unusable: later
        calls raise immediately instead of retrying a broken child.
    """

    def __init__(
        self,
        target: str | Path,
        kind: str = "vectra",
        binary: str | None = None,
        repo: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown mcp backend {kind!r}, expected one of {KINDS}")
        self._target = str(target)
        self._kind: str = kind
        self._binary = binary or _DEFAULT_BINARY[kind]
        self._repo = repo
        self._timeout = timeout
        self._available = True
        self._proc: subprocess.Popen | None = None
        self._reader: _LineReader | None = None
        self._tools: tuple[str, ...] = ()
        self._next_id = 1

    @property
    def kind(self) -> str:
        """Which backend this client talks to."""
        return self._kind

    @property
    def available(self) -> bool:
        """False once the server errored, timed out, or the client was closed."""
        return self._available

    def start(self) -> None:
        """Spawn the child and run the handshake. Idempotent.

        Raises
        ------
        McpError
            The child could not be spawned or did not complete the handshake.
        """
        if self._proc is not None:
            return
        if not self._available:
            raise McpError(f"mcp server {self._binary} is unavailable")
        # 整个握手（含 spawn）都在 try 内：二进制缺失同理其中一种“不可用”，
        # 冒 raw FileNotFoundError 出去就破坏了 fail-open 约定。
        try:
            self._proc = self._spawn()
            self._reader = _LineReader(self._proc.stdout)
            self._request("initialize", self._client_info())
            self._notify("notifications/initialized")
            self._tools = self._list_tools()
        except McpError:
            raise
        except Exception as exc:  # noqa: BLE001 — 二进制缺失/stdout 不可读等一律降级
            self._disable(f"handshake failed: {exc}")
            raise McpError(f"mcp handshake failed for {self._binary}: {exc}") from exc

    def has_tool(self, name: str) -> bool:
        """Return True when the server advertised ``name``."""
        self.start()
        return name in self._tools

    def call(
        self, name: str, arguments: dict[str, Any] | None = None, timeout: float | None = None
    ) -> McpResult:
        """Call one tool and return its structured content.

        Raises
        ------
        McpError
            The tool failed, answered ``isError``, or answered without
            ``structuredContent`` (the Go servers always include it for these
            tools, so its absence means we are talking to something else).
        """
        self.start()
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}}, timeout)
        if result.get("isError"):
            self._disable(f"tool {name} answered isError")
            raise McpError(f"tool {name} failed: {_error_text(result)}")
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            self._disable(f"tool {name} answered without structuredContent")
            raise McpError(f"tool {name} answered without structuredContent")
        return McpResult.from_payload(payload)

    def cypher(self, query: str, limit: int = 200, repo: str | None = None) -> list[dict]:
        """Run a Cypher-subset query and return its rows.

        Parameters
        ----------
        query:
            Statement from the Porto-side template library; never LLM-authored.
        limit:
            Maximum rows.
        repo:
            Overrides the client's repository (multi-repo backend only).
        """
        arguments = {"query": query, "limit": limit}
        served = self._served_repo(repo)
        if served is not None:
            arguments = {"repo": served, **arguments}
        return self.call("cypher", arguments).rows

    def get_node(self, key: str, repo: str | None = None) -> dict[str, Any] | None:
        """Return ``{key,label,props}`` for ``key``, or ``None`` when absent."""
        arguments: dict[str, Any] = {"key": key}
        served = self._served_repo(repo)
        if served is not None:
            arguments = {"repo": served, **arguments}
        payload = self.call("get_node", arguments).payload
        return payload if payload.get("key") else None

    def close(self) -> None:
        """Terminate the child process; the client stays unusable afterwards."""
        self._available = False
        self._kill()

    # --- internals -------------------------------------------------------

    def _served_repo(self, repo: str | None) -> str | None:
        """Repository name to forward, if the backend takes one."""
        if self._kind != "scv-graph":
            return None
        return repo or self._repo

    def _request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send one request and return its ``result`` object."""
        self.start()
        request_id = self._next_id
        self._next_id += 1
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        budget = self._timeout if timeout is None else timeout
        deadline = time.monotonic() + budget
        self._write(frame)
        try:
            message = self._await_id(request_id, deadline)
        except TimeoutError as exc:
            self._disable(f"{method} timed out after {budget}s")
            raise McpError(f"timeout waiting for {method} after {budget}s") from exc
        except McpError as exc:
            self._disable(str(exc))
            raise
        error = message.get("error")
        if error is not None:
            self._disable(f"{method} answered {error}")
            raise McpError(f"{method} failed: {_error_message(error)}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def _await_id(self, request_id: int, deadline: float) -> dict[str, Any]:
        """Read until the frame addressed to ``request_id`` arrives."""
        reader = self._reader
        if reader is None:  # pragma: no cover — start() 保证非空
            raise McpError(f"mcp server {self._binary} is not running")
        while True:
            wait = deadline - time.monotonic()
            if wait <= 0:
                raise TimeoutError("deadline exceeded")
            line = reader.get(wait)
            if line is None:
                self._kill()
                raise McpError(f"mcp server {self._binary} closed its stdout")
            try:
                frame = json.loads(line)
            except ValueError:
                continue  # 服务器偶发打印非 JSON 行（panic 日志等），跳过
            if not isinstance(frame, dict) or frame.get("id") != request_id:
                continue  # 通知与过期应答都跳过，只认自己的 id
            return frame

    def _write(self, frame: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            self._kill()
            raise McpError(f"mcp server {self._binary} exited")
        try:
            proc.stdin.write(json.dumps(frame) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise McpError(f"cannot write to {self._binary}: {exc}") from exc

    def _notify(self, method: str) -> None:
        """Send a notification (no id, no reply) and swallow write failures."""
        try:
            self._write({"jsonrpc": "2.0", "method": method})
        except McpError as exc:
            logger.warning("mcp notification %s failed: %s", method, exc)

    def _list_tools(self) -> tuple[str, ...]:
        tools = self._request("tools/list", {}).get("tools")
        if not isinstance(tools, list):
            return ()
        return tuple(t.get("name", "") for t in tools if isinstance(t, dict))

    def _client_info(self) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "porto", "version": "0"},
        }

    def _spawn(self) -> subprocess.Popen:
        argv = mcp_argv(self._binary, self._kind, self._target)
        return subprocess.Popen(  # noqa: S603 — argv 由本模块按后端契约装配
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def _disable(self, reason: str) -> None:
        """Mark the client unusable and reap the child."""
        if self._available:
            logger.warning("mcp unavailable (%s): %s", self._binary, reason)
        self._available = False
        self._kill()

    def _kill(self) -> None:
        proc, self._proc, self._reader = self._proc, None, None
        if proc is None or getattr(proc, "stdin", None) is None:
            return
        for closer in (proc.stdin.close, proc.terminate):
            try:
                closer()
            except Exception:  # noqa: BLE001 — 子进程已死，回收失败无需上报
                pass
        try:
            proc.wait(timeout=1.0)
        except Exception:  # noqa: BLE001 — 卡住的子进程最后用 kill 兜底
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def _error_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts = [str(c.get("text", "")) for c in content if isinstance(c, dict)]
        return "; ".join(p for p in parts if p) or "no detail"
    return "no detail"


def _error_message(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


def _as_node(value: Any) -> GraphNode | None:
    """Convert a ``{key,label,props}`` map into a :class:`GraphNode`."""
    if not isinstance(value, dict) or "key" not in value:
        return None
    props = value.get("props")
    return GraphNode(
        key=str(value.get("key")),
        label=str(value.get("label") or ""),
        props=dict(props) if isinstance(props, dict) else {},
    )


def _lit(value: object) -> str:
    """Render a value as a Cypher literal (single quotes escaped)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _hop(depth: int, label: str | None) -> str:
    """Edge pattern for one hop window; no variable — long hops may not bind."""
    relationship = f":{label}" if label else ""
    if depth == 1:
        return f"[{relationship}]"
    return f"[{relationship}*{depth}..{depth}]"


class McpStructureGraph:
    """StructureGraph backed by a live MCP server (Tier 1).

    Read traversal (``node`` / ``neighbors`` / ``bfs`` / ``lookup``) runs through
    Cypher templates rather than the servers' own ``neighbors`` / ``bfs`` /
    ``lookup_property`` tools: those return bare keys and would cost one
    ``get_node`` round trip per hit, while ``RETURN n`` hands back whole node
    maps in a single call — and only the multi-repo backend lacks them anyway.
    ``stats`` and ``cycles`` prefer their native counterparts, the Cypher subset
    having no way to compute strongly connected components.

    Parameters
    ----------
    client:
        Client owning the serving child process. Its lifetime outlives this
        adapter; the registry owns closing it.
    repo:
        Repository to query, forwarded by the multi-repo backend only.
    """

    def __init__(self, client: McpGraphClient, repo: str | None = None) -> None:
        self._client = client
        self._repo = repo

    def node(self, key: str) -> GraphNode | None:
        """Return the node with ``key``, or ``None`` when absent or unreachable."""
        try:
            raw = self._client.get_node(key, self._repo)
        except McpError as exc:
            logger.warning("mcp get_node failed: %s", exc)
            return None
        return _as_node(raw)

    def find(self, text: str, limit: int = 20) -> list[GraphNode]:
        """Return nodes whose key mentions ``text``.

        The Cypher subset has no ``toLower()``, and ``CONTAINS`` is
        case-sensitive (verified against a live server: ``'order'`` hits
        ``bean:orderController`` while ``'Order'`` hits ``type:com.acme.
        OrderService``). Each token is therefore tried in its lower-case and
        capitalised form and the hits are unioned. That still misses keys whose
        inner casing differs from the query's, so this is best-effort anchor
        discovery, not a search guarantee — callers degrade to fewer anchors.
        """
        tokens = ident_tokens(text)
        if not tokens:
            return []
        needs = [t for token in tokens[:3] for t in (token, token.capitalize())]
        merged: dict[str, GraphNode] = {}
        for needle in dict.fromkeys(needs):
            pattern = f"MATCH (n) WHERE n.key CONTAINS {_lit(needle)} RETURN n"
            for node in self._query_nodes(pattern, limit=limit):
                merged.setdefault(node.key, node)
            if len(merged) >= limit:
                break
        return sorted(merged.values(), key=lambda n: (len(n.key), n.key))[:limit]

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
            Edge-label filter; ``None`` keeps every label.

        Returns
        -------
        list[GraphNode]
            Adjacent nodes, deduplicated and ordered by key. Empty when the
            server is unreachable.
        """
        directions = ("out", "in") if direction == "both" else (direction,)
        anchor = f"(a {{key:{_lit(key)}}})"
        wanted = tuple(labels) if labels else (None,)
        found: dict[str, GraphNode] = {}
        for way in directions:
            for label in wanted:
                hop = _hop(1, label)
                pattern = f"MATCH {anchor}-{hop}->(b) RETURN b"
                if way == "in":
                    pattern = f"MATCH (b)-{hop}->{anchor} RETURN b"
                for node in self._query_nodes(pattern):
                    found.setdefault(node.key, node)
        return [found[k] for k in sorted(found)]

    def bfs(
        self,
        keys: Sequence[str],
        max_depth: int = 2,
        labels: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[tuple[GraphNode, int]]:
        """Breadth-first expansion from ``keys``, one query per hop distance.

        Parameters
        ----------
        keys:
            Seed node keys; returned as the impact surface, never as results.
        max_depth:
            Maximum hop count. Windows are queried as ``*d..d`` because the
            Cypher subset reads variable-length hops as shortest distance.
        labels:
            Edge-label filter, defaulting to ``("INJECTS",)``.
        limit:
            Hard cap on returned rows.

        Returns
        -------
        list[tuple[GraphNode, int]]
            ``(node, depth)`` pairs ordered by ``(depth, key)``.
        """
        wanted = tuple(labels) if labels else ("INJECTS",)
        seen = {str(key) for key in keys}
        rows: list[tuple[GraphNode, int]] = []
        for depth in range(1, max_depth + 1):
            level: dict[str, GraphNode] = {}
            for seed in keys:
                anchor = f"(a {{key:{_lit(seed)}}})"
                for label in wanted:
                    hop = _hop(depth, label)
                    for node in self._query_nodes(f"MATCH {anchor}-{hop}->(b) RETURN b"):
                        level.setdefault(node.key, node)
            fresh = [(node, depth) for key, node in sorted(level.items()) if key not in seen]
            seen.update(node.key for node, _ in fresh)
            rows.extend(fresh)
            if len(rows) >= limit:
                break
        return rows[:limit]

    def cycles(self) -> list[tuple[tuple[str, ...], str]]:
        """Return dependency cycles reported by the server's ``cycles`` tool.

        Returns
        -------
        list[tuple[tuple[str, ...], str]]
            One ``(members, "tolerated")`` per cycle, members sorted. The MCP
            tool exposes membership and intra-cycle edges but **not** edge
            properties, so the ``via``-based ``"fatal"`` grade of Tier 0 is not
            decidable here; use Tier 0 when grading matters. Empty when the
            server has no ``cycles`` tool or is unreachable.
        """
        try:
            if not self._client.has_tool("cycles"):
                return []
            groups = self._client.call("cycles").payload.get("cycles")
        except McpError as exc:
            logger.warning("mcp cycles failed: %s", exc)
            return []
        found: list[tuple[tuple[str, ...], str]] = []
        for members in groups or ():
            if isinstance(members, list):
                found.append((tuple(sorted(str(m) for m in members)), "tolerated"))
        found.sort()
        return found

    def lookup(self, prop_key: str, prop_value: object, limit: int = 50) -> list[GraphNode]:
        """Return nodes whose ``prop_key`` equals ``prop_value``."""
        pattern = f"MATCH (n {{{prop_key}:{_lit(prop_value)}}}) RETURN n"
        return self._query_nodes(pattern, limit=limit)

    def stats(self) -> GraphStats:
        """Return size and health counters.

        Prefers the server's ``stats`` tool (single call, SCC fields included);
        otherwise counts nodes and edges with two aggregate queries, leaving
        ``sccs`` / ``cyclic_nodes`` at 0 because the Cypher subset cannot
        compute components. Returns an empty :class:`GraphStats` when the
        server is unreachable.
        """
        try:
            if self._client.has_tool("stats"):
                payload = self._client.call("stats").payload
                return GraphStats(
                    nodes=int(payload.get("nodes") or 0),
                    edges=int(payload.get("edges") or 0),
                    sccs=int(payload.get("sccs") or 0),
                    cyclic_nodes=int(payload.get("cyclicNodes") or 0),
                )
            return GraphStats(
                nodes=self._count("MATCH (n) RETURN count(*)"),
                edges=self._count("MATCH (a)-[e]->(b) RETURN count(*)"),
            )
        except McpError as exc:
            logger.warning("mcp stats failed: %s", exc)
            return GraphStats()

    def cypher(self, query: str, limit: int = 200) -> list[dict] | None:
        """Run ``query`` and return rows, or ``None`` when Tier 1 is unusable."""
        try:
            return self._client.cypher(query, limit=limit, repo=self._repo)
        except McpError as exc:
            logger.warning("mcp cypher failed: %s", exc)
            return None

    # --- internals -------------------------------------------------------

    def _query_nodes(self, pattern: str, limit: int = 50) -> list[GraphNode]:
        """Run one ``RETURN n`` template and materialise its nodes."""
        try:
            rows = self._client.cypher(pattern, limit=limit, repo=self._repo)
        except McpError as exc:
            logger.warning("mcp traversal failed: %s", exc)
            return []
        nodes: list[GraphNode] = []
        for row in rows:
            node = next((_as_node(cell) for cell in row.values() if isinstance(cell, dict)), None)
            if node is not None:
                nodes.append(node)
            if len(nodes) >= limit:
                break
        return nodes

    def _count(self, query: str) -> int:
        """Return the single aggregate value produced by ``query``."""
        for row in self._client.cypher(query, limit=1, repo=self._repo):
            for value in row.values():
                if isinstance(value, (int, float)):
                    return int(value)
        return 0
