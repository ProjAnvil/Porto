"""Tests for the Tier 1 structure-graph channel: JSON-RPC over a stdio MCP child.

The suite never spawns a real Go binary (CI must not depend on it): a
``_FakeProc`` answers ``initialize`` / ``tools/list`` / ``tools/call`` exactly
the way the Go MCP SDK does, including notifications that carry no ``id``.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
from typing import Any

import pytest

from porto_chatbot.graph.base import GraphNode, GraphStats, StructureGraph
from porto_chatbot.graph.mcp_client import (
    McpError,
    McpGraphClient,
    McpStructureGraph,
    mcp_argv,
)

_PROTOCOL = "2025-06-18"
_VECTRA_TOOLS = (
    "cypher",
    "get_node",
    "neighbors",
    "bfs",
    "shortest_path",
    "reachable",
    "cycles",
    "stats",
    "lookup_property",
)
_SCV_TOOLS = ("repos", "cypher", "get_node", "refresh")

_NODE = {"key": "bean:orderService", "label": "Bean", "props": {"stereotype": "Service"}}
_PAY = {"key": "bean:payService", "label": "Bean", "props": {"stereotype": "Service"}}
_CYPHER_RESULT = {"columns": ["n"], "rows": [{"n": _NODE}], "count": 1, "total": 1}


# --------------------------------------------------------------------------- #
# Fake MCP server
# --------------------------------------------------------------------------- #


class _FakeStdin:
    """Buffers writes and hands whole JSON lines to the fake server."""

    def __init__(self, proc: _FakeProc) -> None:
        self._proc = proc
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            self._proc.pending.put(json.loads(line))
        return len(text)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeStdout:
    """Yields one reply per request, modelling a real line-oriented stdio peer."""

    def __init__(self, proc: _FakeProc) -> None:
        self._proc = proc

    def __iter__(self):
        while True:
            request = self._proc.pending.get()
            if request is None:  # terminate() queues the EOF sentinel
                return
            if self._proc.hang:
                self._proc.never.wait()  # never set → the client hits its deadline
                return
            yield from (json.dumps(m) + "\n" for m in self._proc.extra_messages(request))
            reply = self._proc.answer(request)
            if reply is not None:
                yield json.dumps(reply) + "\n"


class _FakeProc:
    """Minimal fake MCP server answering initialize / tools/list / tools/call."""

    def __init__(
        self,
        *,
        tools: tuple[str, ...] = _VECTRA_TOOLS,
        payloads: dict[str, dict[str, Any]] | None = None,
        error_tools: set[str] | None = None,
        plain_tools: set[str] | None = None,
        extra: list[dict[str, Any]] | None = None,
        hang: bool = False,
    ) -> None:
        self.tools = list(tools)
        self.payloads = payloads or {}
        self.error_tools = error_tools or set()
        self.plain_tools = plain_tools or set()
        self.extra = list(extra or [])
        self.hang = hang
        self.received: list[dict[str, Any]] = []
        self.terminated = False
        self.pending: queue.SimpleQueue = queue.SimpleQueue()
        self.never = threading.Event()
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout(self)

    def extra_messages(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        """Unsolicited frames pushed to the client before this request's reply."""
        messages, self.extra = list(self.extra), []
        return messages

    def answer(self, request: dict[str, Any]) -> dict[str, Any] | None:
        self.received.append(request)
        method = request.get("method")
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "serverInfo": {"name": "fake", "version": "0"},
                    "protocolVersion": _PROTOCOL,
                },
            }
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"tools": [{"name": n} for n in self.tools]},
            }
        if method == "tools/call":
            return self._tool_result(request)
        return None  # notifications get no reply

    def _tool_result(self, request: dict[str, Any]) -> dict[str, Any]:
        name = request["params"]["name"]
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request["id"]}
        if name in self.error_tools:
            frame["result"] = {"content": [{"type": "text", "text": "boom"}], "isError": True}
        elif name in self.plain_tools:
            frame["result"] = {"content": [{"type": "text", "text": "{}"}]}
        else:
            payload = self.payloads.get(name, {})
            if callable(payload):  # per-request answers (e.g. successive counts)
                payload = payload(request["params"].get("arguments") or {})
            frame["result"] = {"structuredContent": dict(payload)}
        return frame

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.pending.put(None)

    kill = terminate


def _spawn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str = "vectra",
    repo: str | None = None,
    timeout: float = 1.0,
    **fake_kwargs: Any,
) -> tuple[McpGraphClient, _FakeProc, list[list[str]]]:
    """Build a client whose subprocess.Popen returns a fake MCP server."""
    fake = _FakeProc(**fake_kwargs)
    argv_box: list[list[str]] = []

    def _popen(argv: list[str], **kwargs: Any) -> _FakeProc:
        argv_box.append(list(argv))
        return fake

    monkeypatch.setattr(subprocess, "Popen", _popen)
    client = McpGraphClient(
        binary="fake-bin",
        target="/kb/alpha",
        kind=kind,
        repo=repo,
        timeout=timeout,
    )
    return client, fake, argv_box


def _calls(fake: _FakeProc) -> list[dict[str, Any]]:
    return [m for m in fake.received if m.get("method") == "tools/call"]


def _call_args(fake: _FakeProc, name: str) -> dict[str, Any]:
    return [m for m in _calls(fake) if m["params"]["name"] == name][-1]["params"]["arguments"]


def _queries(fake: _FakeProc) -> list[str]:
    return [m["params"]["arguments"]["query"] for m in _calls(fake) if m["params"]["name"] == "cypher"]


# --------------------------------------------------------------------------- #
# argv construction
# --------------------------------------------------------------------------- #


def test_mcp_argv_uses_analysis_dir_for_scv_graph():
    assert mcp_argv("/bin/scv-graph-mcp", "scv-graph", "/kb") == [
        "/bin/scv-graph-mcp",
        "-analysis-dir",
        "/kb",
    ]


def test_mcp_argv_uses_snapshot_for_vectra():
    assert mcp_argv("/bin/vectra-mcp", "vectra", "/kb/alpha/structure.vg") == [
        "/bin/vectra-mcp",
        "-snapshot",
        "/kb/alpha/structure.vg",
    ]


# --------------------------------------------------------------------------- #
# Handshake / transport
# --------------------------------------------------------------------------- #


def test_handshake_happens_once_for_many_calls(monkeypatch):
    client, fake, argv = _spawn(monkeypatch, payloads={"cypher": _CYPHER_RESULT})
    client.start()
    client.cypher("MATCH (n) RETURN n")
    client.cypher("MATCH (n) RETURN n")

    assert len(argv) == 1  # 一个仓库会话只起一个子进程
    assert [m["method"] for m in fake.received] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
        "tools/call",
    ]


def test_cypher_without_start_still_handshakes(monkeypatch):
    client, fake, _ = _spawn(monkeypatch, payloads={"cypher": _CYPHER_RESULT})
    assert client.cypher("MATCH (n) RETURN n") == [{"n": _NODE}]
    assert fake.received[0]["params"]["protocolVersion"] == _PROTOCOL


def test_initialized_notification_carries_no_id(monkeypatch):
    client, fake, _ = _spawn(monkeypatch, payloads={"cypher": _CYPHER_RESULT})
    client.start()
    assert fake.received[1] == {"jsonrpc": "2.0", "method": "notifications/initialized"}


def test_responses_are_matched_by_id_and_notifications_skipped(monkeypatch):
    stray = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    client, fake, _ = _spawn(monkeypatch, payloads={"cypher": _CYPHER_RESULT}, extra=[stray])
    assert client.cypher("MATCH (n) RETURN n") == [{"n": _NODE}]


def test_scv_graph_spawn_uses_analysis_dir_flag(monkeypatch):
    client, fake, argv = _spawn(monkeypatch, kind="scv-graph", payloads={"cypher": _CYPHER_RESULT})
    client.start()
    assert argv[0] == ["fake-bin", "-analysis-dir", "/kb/alpha"]


def test_vectra_spawn_uses_snapshot_flag(monkeypatch):
    client, fake, argv = _spawn(monkeypatch, kind="vectra", payloads={"cypher": _CYPHER_RESULT})
    client.start()
    assert argv[0] == ["fake-bin", "-snapshot", "/kb/alpha"]


# --------------------------------------------------------------------------- #
# Per-backend tool arguments
# --------------------------------------------------------------------------- #


def test_scv_graph_cypher_arguments_carry_repo(monkeypatch):
    client, fake, _ = _spawn(monkeypatch, kind="scv-graph", repo="alpha", payloads={"cypher": _CYPHER_RESULT})
    client.cypher("MATCH (n) RETURN n", limit=7)
    assert _call_args(fake, "cypher") == {
        "repo": "alpha",
        "query": "MATCH (n) RETURN n",
        "limit": 7,
    }


def test_vectra_cypher_arguments_omit_repo(monkeypatch):
    client, fake, _ = _spawn(monkeypatch, kind="vectra", repo="alpha", payloads={"cypher": _CYPHER_RESULT})
    client.cypher("MATCH (n) RETURN n")
    assert _call_args(fake, "cypher") == {"query": "MATCH (n) RETURN n", "limit": 200}


def test_scv_graph_get_node_arguments_carry_repo(monkeypatch):
    client, fake, _ = _spawn(
        monkeypatch, kind="scv-graph", repo="alpha", payloads={"get_node": dict(_NODE)}
    )
    assert client.get_node("bean:orderService") == _NODE
    assert _call_args(fake, "get_node") == {"repo": "alpha", "key": "bean:orderService"}


def test_vectra_get_node_arguments_omit_repo(monkeypatch):
    client, fake, _ = _spawn(monkeypatch, kind="vectra", payloads={"get_node": dict(_NODE)})
    client.get_node("bean:orderService")
    assert _call_args(fake, "get_node") == {"key": "bean:orderService"}


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


def test_tool_error_raises_and_disables_the_client(monkeypatch):
    client, fake, _ = _spawn(
        monkeypatch, payloads={"cypher": _CYPHER_RESULT}, error_tools={"cypher"}
    )
    with pytest.raises(McpError, match="cypher"):
        client.cypher("MATCH (n) RETURN n")
    assert client.available is False

    seen = len(fake.received)
    with pytest.raises(McpError):
        client.cypher("MATCH (n) RETURN n")
    assert len(fake.received) == seen  # 坏掉的服务不再被打扰


def test_missing_structured_content_is_an_error(monkeypatch):
    client, fake, _ = _spawn(monkeypatch, plain_tools={"cypher"})
    with pytest.raises(McpError, match="structuredContent"):
        client.cypher("MATCH (n) RETURN n")
    assert client.available is False


def test_read_timeout_raises_and_kills_the_child(monkeypatch):
    client, fake, _ = _spawn(monkeypatch, hang=True, timeout=0.05)
    with pytest.raises(McpError, match="timeout"):
        client.cypher("MATCH (n) RETURN n")
    assert client.available is False
    assert fake.terminated is True  # 挂死的子进程必须被回收，避免泄漏


def test_missing_binary_is_unavailable_not_an_oserror(monkeypatch):
    def _popen(argv: list[str], **kwargs: Any):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "Popen", _popen)
    client = McpGraphClient(target="/kb/alpha", kind="vectra", timeout=0.5)
    with pytest.raises(McpError, match="handshake"):
        client.start()
    assert client.available is False


def test_close_terminates_the_child(monkeypatch):
    client, fake, _ = _spawn(monkeypatch, payloads={"cypher": _CYPHER_RESULT})
    client.start()
    client.close()
    assert fake.terminated is True
    assert client.available is False


# --------------------------------------------------------------------------- #
# StructureGraph adapter
# --------------------------------------------------------------------------- #


def test_adapter_implements_the_structure_graph_protocol(monkeypatch):
    client, _, _ = _spawn(monkeypatch, payloads={"cypher": _CYPHER_RESULT})
    assert isinstance(McpStructureGraph(client), StructureGraph)


def test_adapter_node_returns_none_when_missing(monkeypatch):
    client, _, _ = _spawn(monkeypatch, error_tools={"get_node"})
    assert McpStructureGraph(client).node("bean:nope") is None


def test_adapter_neighbors_walks_out_edges_with_labels(monkeypatch):
    client, fake, _ = _spawn(monkeypatch, payloads={"cypher": _CYPHER_RESULT})
    graph = McpStructureGraph(client, repo="alpha")
    nodes = graph.neighbors("bean:orderController", labels=["INJECTS"])
    assert [n.key for n in nodes] == ["bean:orderService"]
    assert _queries(fake)[-1] == "MATCH (a {key:'bean:orderController'})-[:INJECTS]->(b) RETURN b"


def test_adapter_neighbors_in_direction_reverses_the_pattern(monkeypatch):
    client, fake, _ = _spawn(monkeypatch, payloads={"cypher": _CYPHER_RESULT})
    McpStructureGraph(client).neighbors("bean:orderService", direction="in")
    assert _queries(fake)[-1] == "MATCH (b)-[]->(a {key:'bean:orderService'}) RETURN b"


def test_adapter_bfs_queries_one_depth_at_a_time(monkeypatch):
    client, fake, _ = _spawn(monkeypatch, payloads={"cypher": _CYPHER_RESULT})
    rows = McpStructureGraph(client).bfs(["bean:orderController"], max_depth=2, labels=["INJECTS"])
    assert rows == [(GraphNode(key=_NODE["key"], label=_NODE["label"], props=_NODE["props"]), 1)]
    assert _queries(fake) == [
        "MATCH (a {key:'bean:orderController'})-[:INJECTS]->(b) RETURN b",
        "MATCH (a {key:'bean:orderController'})-[:INJECTS*2..2]->(b) RETURN b",
    ]


def test_adapter_lookup_matches_a_property(monkeypatch):
    client, fake, _ = _spawn(monkeypatch, payloads={"cypher": _CYPHER_RESULT})
    McpStructureGraph(client).lookup("stereotype", "Service", limit=3)
    assert _queries(fake)[-1] == "MATCH (n {stereotype:'Service'}) RETURN n"


def test_adapter_stats_prefers_the_native_tool(monkeypatch):
    payload = {"nodes": 5, "edges": 4, "sccs": 4, "cyclicNodes": 2, "sealed": True}
    client, fake, _ = _spawn(monkeypatch, payloads={"stats": payload})
    assert McpStructureGraph(client).stats() == GraphStats(
        nodes=5, edges=4, index_keys=(), sccs=4, cyclic_nodes=2
    )
    assert [c["params"]["name"] for c in _calls(fake)] == ["stats"]


def test_adapter_stats_falls_back_to_cypher_counts(monkeypatch):
    counts = iter(
        [
            {"columns": ["count(*)"], "rows": [{"count(*)": 5}], "count": 1, "total": 1},
            {"columns": ["count(*)"], "rows": [{"count(*)": 4}], "count": 1, "total": 1},
        ]
    )
    client, fake, _ = _spawn(
        monkeypatch, tools=_SCV_TOOLS, payloads={"cypher": lambda _args: next(counts)}
    )
    stats = McpStructureGraph(client).stats()
    assert (stats.nodes, stats.edges) == (5, 4)
    assert (stats.sccs, stats.cyclic_nodes) == (0, 0)
    assert _queries(fake) == ["MATCH (n) RETURN count(*)", "MATCH (a)-[e]->(b) RETURN count(*)"]


def test_adapter_cycles_needs_the_native_tool(monkeypatch):
    payload = {"cycles": [["bean:orderService", "bean:payService"]]}
    client, _, _ = _spawn(monkeypatch, payloads={"cycles": payload})
    cycles = McpStructureGraph(client).cycles()
    assert cycles == [(("bean:orderService", "bean:payService"), "tolerated")]
    empty, _, _ = _spawn(monkeypatch, tools=_SCV_TOOLS)
    assert McpStructureGraph(empty).cycles() == []


def test_adapter_cypher_returns_rows_and_none_on_failure(monkeypatch):
    client, _, _ = _spawn(monkeypatch, payloads={"cypher": _CYPHER_RESULT})
    graph = McpStructureGraph(client)
    assert graph.cypher("MATCH (n) RETURN n") == [{"n": _NODE}]

    broken, _, _ = _spawn(monkeypatch, error_tools={"cypher"})
    assert McpStructureGraph(broken).cypher("MATCH (n) RETURN n") is None


def test_adapter_methods_fail_open(monkeypatch):
    client, _, _ = _spawn(monkeypatch, error_tools={"get_node", "cypher", "stats", "cycles"})
    graph = McpStructureGraph(client)
    assert graph.node("bean:orderService") is None
    assert graph.neighbors("bean:orderService") == []
    assert graph.bfs(["bean:orderService"]) == []
    assert graph.lookup("stereotype", "Service") == []
    assert graph.cycles() == []
    assert graph.stats() == GraphStats()
