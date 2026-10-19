"""Tests for structure-graph context rendering (prompt-facing, fail-open)."""
from __future__ import annotations

from pathlib import Path

import pytest

from porto_chatbot.graph import discovery
from porto_chatbot.graph.context import (
    build_graph_context,
    build_identify_summary,
    build_subsystem_impact,
)
from porto_chatbot.graph.discovery import StructureGraphRegistry, reset_graph_caches
from porto_chatbot.models import SourceChunk, Subsystem
from porto_chatbot.settings import Settings

_FIX = Path(__file__).parent / "fixtures" / "structure.vg"


@pytest.fixture(autouse=True)
def _no_binaries(monkeypatch):
    """Keep these cases on Tier 0 regardless of what the host has installed."""
    monkeypatch.setattr(discovery, "find_scv_graph_mcp", lambda: None)
    monkeypatch.setattr(discovery, "find_vectra_mcp", lambda: None)
    yield
    reset_graph_caches()


@pytest.fixture()
def kb(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    return root


def _repo(kb: Path, name: str = "alpha") -> Path:
    folder = kb / name
    folder.mkdir()
    (folder / "structure.vg").write_bytes(_FIX.read_bytes())
    return folder


def _settings(kb: Path, **overrides) -> Settings:
    base: dict = {"kb_dirs": [kb], "graph_enabled": True, "graph_mcp_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _chunk(root: str = "alpha", path: str = "alpha/ARCHITECTURE.md") -> SourceChunk:
    return SourceChunk(id="c1", path=path, title="OrderController", text="orders",
                       metadata={"root": root})


# --- find(): the node-search primitive ------------------------------------ #


def test_find_matches_node_keys_by_token(kb):
    _repo(kb)
    graph = StructureGraphRegistry(_settings(kb)).graph_for("alpha")

    keys = [n.key for n in graph.find("order", limit=10)]
    assert "bean:orderController" in keys
    assert "bean:orderService" in keys


def test_find_returns_nothing_for_unknown_text(kb):
    _repo(kb)
    graph = StructureGraphRegistry(_settings(kb)).graph_for("alpha")

    assert graph.find("zzzznotathing", limit=10) == []


# --- build_graph_context -------------------------------------------------- #


def test_graph_context_renders_the_impact_surface(kb):
    _repo(kb)
    registry = StructureGraphRegistry(_settings(kb))

    chunks = build_graph_context([_chunk()], "orderservice", _settings(kb), registry)

    assert len(chunks) == 1
    rendered = chunks[0]
    assert rendered.id == "graph:alpha"
    assert rendered.metadata["source"] == "vectra"
    assert "bean:orderService" in rendered.text
    assert "INJECTS" in rendered.text


def test_graph_context_is_empty_without_a_snapshot(kb):
    registry = StructureGraphRegistry(_settings(kb))

    assert build_graph_context([_chunk()], "anything", _settings(kb), registry) == []


def test_graph_context_is_empty_when_disabled(kb):
    _repo(kb)
    registry = StructureGraphRegistry(_settings(kb, graph_enabled=False))

    assert build_graph_context([_chunk()], "order", _settings(kb, graph_enabled=False),
                               registry) == []


def test_graph_context_survives_a_broken_registry(kb):
    _repo(kb)

    class _Boom:
        def graph_for(self, repo):
            raise RuntimeError("registry exploded")

    assert build_graph_context([_chunk()], "order", _settings(kb), _Boom()) == []


def test_graph_context_ignores_chunks_without_a_root(kb):
    _repo(kb)
    registry = StructureGraphRegistry(_settings(kb))
    orphan = SourceChunk(id="c2", path="loose.md", title="t", text="t", metadata={})

    assert build_graph_context([orphan], "order", _settings(kb), registry) == []


# --- build_identify_summary ----------------------------------------------- #


def test_identify_summary_reports_the_structure_shape(kb):
    _repo(kb)
    registry = StructureGraphRegistry(_settings(kb))

    summary = build_identify_summary(_settings(kb), registry)

    assert summary
    assert "alpha" in summary
    assert "Service" in summary  # stereotype 普查


def test_identify_summary_is_empty_without_a_snapshot(kb):
    registry = StructureGraphRegistry(_settings(kb))

    assert build_identify_summary(_settings(kb), registry) == ""


def test_identify_summary_survives_a_broken_registry(kb):
    class _Boom:
        def graph_for(self, repo):
            raise RuntimeError("registry exploded")

    assert build_identify_summary(_settings(kb), _Boom()) == ""


# --- build_subsystem_impact ----------------------------------------------- #


def test_subsystem_impact_renders_reachable_nodes(kb):
    _repo(kb)

    sub = Subsystem(name="orderService", responsibility="orders",
                    capabilities=["place order"])
    impact = build_subsystem_impact(_settings(kb), sub)

    assert impact
    assert "bean:orderService" in impact


def test_subsystem_impact_is_empty_without_a_snapshot(kb):
    sub = Subsystem(name="orderService", responsibility="orders")

    assert build_subsystem_impact(_settings(kb), sub) == ""


def test_subsystem_impact_survives_a_broken_registry(kb, monkeypatch):
    from porto_chatbot.graph import context

    monkeypatch.setattr(
        context, "StructureGraphRegistry",
        lambda _s: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    sub = Subsystem(name="orderService", responsibility="orders")

    assert build_subsystem_impact(_settings(kb), sub) == ""
