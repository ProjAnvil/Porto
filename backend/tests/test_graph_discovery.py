"""Tests for structure-graph capability discovery and tier degradation."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from porto_chatbot.graph import discovery
from porto_chatbot.graph.base import GraphEdge, GraphNode, StructureGraph
from porto_chatbot.graph.discovery import (
    EmptyStructureGraph,
    StructureGraphRegistry,
    find_scv_graph_mcp,
    find_vectra_mcp,
    reset_graph_caches,
    scv_perception_dist,
)
from porto_chatbot.graph.local_graph import LocalStructureGraph
from porto_chatbot.graph.mcp_client import McpError, McpStructureGraph
from porto_chatbot.graph.snapshot import SnapshotData
from porto_chatbot.settings import Settings

_FIX = Path(__file__).parent / "fixtures" / "structure.vg"
_NODE = {"key": "bean:orderService", "label": "Bean", "props": {"stereotype": "Service"}}


@pytest.fixture(autouse=True)
def _clean_caches():
    """module-level caches would otherwise leak between tests."""
    yield
    reset_graph_caches()


@pytest.fixture()
def kb(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    return root


def _settings(kb: Path, **overrides) -> Settings:
    """Settings pinned to one temp knowledge-base directory."""
    return Settings(kb_dirs=[kb], graph_enabled=True, **overrides)


def _repo(kb: Path, name: str = "alpha") -> Path:
    folder = kb / name
    folder.mkdir()
    (folder / "structure.vg").write_bytes(_FIX.read_bytes())
    return folder


class _StubClient:
    """Stand-in for McpGraphClient recording how discovery configured it."""

    instances: list[_StubClient] = []

    def __init__(self, target, kind="vectra", binary=None, repo=None, timeout=10.0):
        self.target = str(target)
        self.kind = kind
        self.binary = binary
        self.repo = repo
        self.timeout = timeout
        self.available = True
        self.failing: Exception | None = None
        self.live_tools = {"cypher", "get_node"}
        self.queries: list[str] = []
        _StubClient.instances.append(self)

    def start(self):
        if self.failing is not None:
            raise self.failing

    def close(self):
        self.available = False

    def has_tool(self, name):
        return name in self.live_tools

    def cypher(self, query, limit=200, repo=None):
        self.queries.append(query)
        return [{"n": _NODE}]

    def get_node(self, key, repo=None):
        return dict(_NODE)


@pytest.fixture()
def stub_client(monkeypatch):
    """Replace McpGraphClient with the stub and hand it out."""
    _StubClient.instances = []
    monkeypatch.setattr(discovery, "McpGraphClient", _StubClient)
    return _StubClient.instances


# --------------------------------------------------------------------------- #
# Tier 0
# --------------------------------------------------------------------------- #


def test_snapshot_is_served_as_the_local_tier(kb):
    _repo(kb, "alpha")
    registry = StructureGraphRegistry(_settings(kb))

    graph = registry.graph_for("alpha")
    assert registry.tier("alpha") == "local"
    assert isinstance(graph, LocalStructureGraph)
    assert graph.node("bean:orderService") is not None
    assert graph.cypher("MATCH (n) RETURN n") is None  # Tier 0 的能力协商信号


def test_missing_repo_falls_back_to_an_empty_graph(kb):
    registry = StructureGraphRegistry(_settings(kb))

    graph = registry.graph_for("nope")
    assert registry.tier("nope") == "empty"
    assert isinstance(graph, EmptyStructureGraph)
    assert graph.node("bean:orderService") is None
    assert graph.neighbors("bean:orderService") == []
    assert graph.bfs(["bean:orderService"]) == []
    assert graph.cycles() == []
    assert graph.lookup("stereotype", "Service") == []
    assert graph.stats().nodes == 0
    assert graph.cypher("MATCH (n) RETURN n") is None


def test_empty_graph_implements_the_protocol():
    assert isinstance(EmptyStructureGraph(), StructureGraph)


def test_loaded_graph_is_cached_until_the_file_changes(kb, monkeypatch):
    folder = _repo(kb, "alpha")
    registry = StructureGraphRegistry(_settings(kb))
    first = registry.graph_for("alpha")
    assert registry.graph_for("alpha") is first

    monkeypatch.setattr(
        discovery,
        "decode_snapshot",
        lambda _buf: SnapshotData(
            nodes=[GraphNode(key="bean:new", label="Bean")],
            edges=[GraphEdge(src="bean:new", dst="bean:new", label="INJECTS")],
            index_keys=[],
        ),
    )
    (folder / "structure.vg").write_text("stale", encoding="utf-8")
    os.utime(folder / "structure.vg", None)  # 同内容不同 mtime → 必须重新加载
    second = registry.graph_for("alpha")
    assert second is not first
    assert second.node("bean:new") is not None


def test_oversized_snapshot_degrades_to_empty(kb):
    _repo(kb, "alpha")
    registry = StructureGraphRegistry(_settings(kb, graph_max_nodes=2))

    assert registry.tier("alpha") == "empty"
    assert registry.graph_for("alpha").stats().nodes == 0


def test_corrupt_snapshot_degrades_to_empty(kb):
    folder = kb / "alpha"
    folder.mkdir()
    (folder / "structure.vg").write_bytes(b"XXXX" + b"\x00" * 16)
    registry = StructureGraphRegistry(_settings(kb))

    assert registry.tier("alpha") == "empty"


def test_disabled_graph_never_touches_the_filesystem(kb):
    _repo(kb, "alpha")
    registry = StructureGraphRegistry(_settings(kb, graph_enabled=False))

    assert registry.tier("alpha") == "empty"


def test_snapshot_is_looked_up_in_every_kb_dir(kb, tmp_path):
    second = tmp_path / "alt"
    second.mkdir()
    alt = _repo(second, "beta")
    registry = StructureGraphRegistry(Settings(kb_dirs=[kb, second]))

    assert registry.tier("beta") == "local"
    assert alt.exists()


def test_repo_names_cannot_escape_the_kb_dir(kb):
    registry = StructureGraphRegistry(_settings(kb))

    assert registry.tier("../../etc") == "empty"


# --------------------------------------------------------------------------- #
# Tier 1
# --------------------------------------------------------------------------- #


def test_scv_graph_server_wins_over_vectra(kb, monkeypatch, stub_client):
    _repo(kb, "alpha")
    monkeypatch.setattr(discovery, "find_scv_graph_mcp", lambda: Path("/dist/scv-graph-mcp"))
    monkeypatch.setattr(discovery, "find_vectra_mcp", lambda: "/usr/bin/vectra-mcp")
    registry = StructureGraphRegistry(_settings(kb))

    assert registry.tier("alpha") == "mcp"
    client = stub_client[0]
    assert (client.kind, client.target, client.repo) == ("scv-graph", str(kb), "alpha")


def test_vectra_is_used_when_scv_has_no_dist_binary(kb, monkeypatch, stub_client):
    folder = _repo(kb, "alpha")
    monkeypatch.setattr(discovery, "find_scv_graph_mcp", lambda: None)
    monkeypatch.setattr(discovery, "find_vectra_mcp", lambda: "/usr/bin/vectra-mcp")
    registry = StructureGraphRegistry(_settings(kb))

    assert registry.tier("alpha") == "mcp"
    client = stub_client[0]
    assert client.kind == "vectra"
    assert client.target == str(folder / "structure.vg")
    assert client.repo is None


def test_explicit_binary_is_preferred(kb, monkeypatch, stub_client):
    _repo(kb, "alpha")
    monkeypatch.setattr(discovery, "find_scv_graph_mcp", lambda: Path("/dist/scv-graph-mcp"))
    monkeypatch.setattr(discovery, "find_vectra_mcp", lambda: "/usr/bin/vectra-mcp")
    registry = StructureGraphRegistry(_settings(kb, graph_mcp_binary="/opt/vectra-mcp"))

    registry.graph_for("alpha")
    assert stub_client[0].binary == "/opt/vectra-mcp"
    assert stub_client[0].kind == "vectra"  # 名字不含 scv-graph → 单快照后端


def test_timeout_setting_reaches_the_client(kb, monkeypatch, stub_client):
    _repo(kb, "alpha")
    monkeypatch.setattr(discovery, "find_scv_graph_mcp", lambda: Path("/dist/scv-graph-mcp"))
    registry = StructureGraphRegistry(_settings(kb, graph_mcp_timeout=3.5))

    registry.graph_for("alpha")
    assert stub_client[0].timeout == 3.5


def test_tier_one_graph_answers_cypher(kb, monkeypatch, stub_client):
    _repo(kb, "alpha")
    monkeypatch.setattr(discovery, "find_scv_graph_mcp", lambda: Path("/dist/scv-graph-mcp"))
    registry = StructureGraphRegistry(_settings(kb))

    graph = registry.graph_for("alpha")
    assert isinstance(graph, McpStructureGraph)
    assert graph.cypher("MATCH (n) RETURN n") == [{"n": _NODE}]


def test_no_binary_at_all_falls_back_to_tier_zero(kb, monkeypatch, stub_client):
    _repo(kb, "alpha")
    monkeypatch.setattr(discovery, "find_scv_graph_mcp", lambda: None)
    monkeypatch.setattr(discovery, "find_vectra_mcp", lambda: None)
    registry = StructureGraphRegistry(_settings(kb))

    assert registry.tier("alpha") == "local"
    assert stub_client == []


def test_vectra_needs_the_repo_snapshot(kb, monkeypatch, stub_client):
    monkeypatch.setattr(discovery, "find_scv_graph_mcp", lambda: None)
    monkeypatch.setattr(discovery, "find_vectra_mcp", lambda: "/usr/bin/vectra-mcp")
    registry = StructureGraphRegistry(_settings(kb))

    assert registry.tier("ghost") == "empty"  # 没有 .vg 就没有 Tier 1 也没有 Tier 0


def test_mcp_disabled_forces_tier_zero(kb, monkeypatch, stub_client):
    _repo(kb, "alpha")
    monkeypatch.setattr(discovery, "find_scv_graph_mcp", lambda: Path("/dist/scv-graph-mcp"))
    registry = StructureGraphRegistry(_settings(kb, graph_mcp_enabled=False))

    assert registry.tier("alpha") == "local"
    assert stub_client == []


def test_handshake_failure_degrades_for_this_and_later_calls(kb, monkeypatch, stub_client):
    _repo(kb, "alpha")
    monkeypatch.setattr(discovery, "find_scv_graph_mcp", lambda: Path("/dist/scv-graph-mcp"))
    registry = StructureGraphRegistry(_settings(kb))

    original_start = _StubClient.start

    def _failing(self):
        self.available = False
        raise McpError("handshake failed")

    monkeypatch.setattr(_StubClient, "start", _failing)
    assert registry.tier("alpha") == "local"
    first_client, count = stub_client[0], len(stub_client)

    assert registry.tier("alpha") == "local"
    assert len(stub_client) == count  # 死掉的连接被记住，不再反复重启
    assert first_client.available is False

    monkeypatch.setattr(_StubClient, "start", original_start)


# --------------------------------------------------------------------------- #
# Binary discovery
# --------------------------------------------------------------------------- #


def test_scv_binary_comes_from_the_dist_root(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    binary = dist / "graphmcp" / "scv-graph-mcp"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("SCV_PERCEPTION_DIST", str(dist))

    assert scv_perception_dist() == dist
    assert find_scv_graph_mcp() == binary


def test_scv_binary_missing_yields_none(tmp_path, monkeypatch):
    monkeypatch.setenv("SCV_PERCEPTION_DIST", str(tmp_path / "empty"))
    assert find_scv_graph_mcp() is None


def test_scv_binary_is_not_looked_up_on_path(tmp_path, monkeypatch):
    """SCV 自己也不用 shutil.which 找它（perceive.py:323-338），我们保持一致。"""
    monkeypatch.setenv("SCV_PERCEPTION_DIST", str(tmp_path / "empty"))
    monkeypatch.setattr(shutil, "which", lambda _name: "/opt/scv-graph-mcp")
    assert find_scv_graph_mcp() is None


def test_vectra_binary_is_looked_up_on_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}" if name == "vectra-mcp" else None)
    assert find_vectra_mcp() == "/usr/bin/vectra-mcp"


def test_vectra_binary_missing_yields_none(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert find_vectra_mcp() is None
