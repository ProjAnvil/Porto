"""End-to-end tests: the graph channel reaching retrieve / identify / generate."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from porto_chatbot.agent.nodes import identify as identify_node
from porto_chatbot.graph import discovery
from porto_chatbot.graph.discovery import reset_graph_caches
from porto_chatbot.models import SourceChunk, Subsystem
from porto_chatbot.models.enums import QueryTransformStrategy
from porto_chatbot.query_transform import retrieve_with_transform
from porto_chatbot.settings import Settings
from porto_chatbot.specs import steps as spec_steps

_FIX = Path(__file__).parent / "fixtures" / "structure.vg"


@pytest.fixture(autouse=True)
def _no_binaries(monkeypatch):
    """Keep these cases on Tier 0 regardless of what the host has installed."""
    monkeypatch.setattr(discovery, "find_scv_graph_mcp", lambda: None)
    monkeypatch.setattr(discovery, "find_vectra_mcp", lambda: None)
    yield
    reset_graph_caches()


def _kb(tmp_path: Path, with_snapshot: bool = True) -> Path:
    root = tmp_path / "kb"
    (root / "alpha").mkdir(parents=True)
    (root / "alpha" / "ARCHITECTURE.md").write_text("# alpha\norders", encoding="utf-8")
    if with_snapshot:
        (root / "alpha" / "structure.vg").write_bytes(_FIX.read_bytes())
    return root


def _settings(kb: Path, **overrides) -> Settings:
    base: dict = {
        "kb_dirs": [kb],
        "graph_enabled": True,
        "graph_mcp_enabled": False,
        "rerank_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


class _Store:
    """Minimal KB store stub: search() returns chunks, _search_raw() the same."""

    def __init__(self, chunks):
        self._chunks = chunks

    def search(self, query, top_k=6):
        return list(self._chunks)

    def _search_raw(self, query, top_k=6):
        return list(self._chunks)


def _chunk() -> SourceChunk:
    return SourceChunk(
        id="c1", path="alpha/ARCHITECTURE.md", title="OrderController",
        text="orders", metadata={"root": "alpha"},
    )


# --- retrieve ------------------------------------------------------------- #


def test_retrieve_appends_a_graph_chunk(tmp_path):
    kb = _kb(tmp_path)
    settings = _settings(kb)

    result = retrieve_with_transform(
        "orderService", QueryTransformStrategy.NONE, _Store([_chunk()]),
        settings, None, 6,
    )

    ids = [c.id for c in result.chunks]
    assert "graph:alpha" in ids
    assert not result.degraded


def test_retrieve_degrades_gracefully_without_a_snapshot(tmp_path):
    kb = _kb(tmp_path, with_snapshot=False)
    settings = _settings(kb)

    result = retrieve_with_transform(
        "orderService", QueryTransformStrategy.NONE, _Store([_chunk()]),
        settings, None, 6,
    )

    assert [c.id for c in result.chunks] == ["c1"]


def test_retrieve_is_unchanged_when_the_graph_is_disabled(tmp_path):
    kb = _kb(tmp_path)
    settings = _settings(kb, graph_enabled=False)

    result = retrieve_with_transform(
        "orderService", QueryTransformStrategy.NONE, _Store([_chunk()]),
        settings, None, 6,
    )

    assert [c.id for c in result.chunks] == ["c1"]


def test_retrieve_injects_on_the_llm_fallback_path(tmp_path):
    """The fail-open branch must not silently lose graph context."""
    import porto_chatbot.query_transform as qt

    kb = _kb(tmp_path)
    settings = _settings(kb)

    class _BrokenLLM:
        enabled = True

        def complete(self, *_a, **_k):
            raise RuntimeError("llm down")

    original = qt._generate_query_variants
    qt._generate_query_variants = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x"))
    try:
        result = retrieve_with_transform(
            "orderService", QueryTransformStrategy.MULTI_QUERY, _Store([_chunk()]),
            settings, _BrokenLLM(), 6,
        )
    finally:
        qt._generate_query_variants = original

    assert result.degraded
    assert "graph:alpha" in [c.id for c in result.chunks]


# --- identify ------------------------------------------------------------- #


def _agent(settings):
    return SimpleNamespace(settings=settings, logger=_Logger())


class _Logger:
    def __init__(self):
        self.exceptions = []

    def exception(self, msg, *a, **k):
        self.exceptions.append(msg)

    def info(self, *a, **k):
        return None


def test_identify_prompt_carries_the_structure_summary(tmp_path):
    kb = _kb(tmp_path)
    block = identify_node._graph_summary_block(_agent(_settings(kb)))

    assert block.startswith("\n\n代码结构图摘要:")
    assert "alpha" in block


def test_identify_prompt_is_unchanged_without_a_snapshot(tmp_path):
    kb = _kb(tmp_path, with_snapshot=False)

    assert identify_node._graph_summary_block(_agent(_settings(kb))) == ""


def test_identify_prompt_is_unchanged_when_disabled(tmp_path):
    kb = _kb(tmp_path)

    assert identify_node._graph_summary_block(_agent(_settings(kb, graph_enabled=False))) == ""


def test_identify_swallows_a_broken_registry(tmp_path):
    kb = _kb(tmp_path)
    agent = _agent(_settings(kb))

    class _Boom:
        def graph_for(self, repo):
            raise RuntimeError("registry exploded")

    # identify 内部是从 graph.discovery 取 registry 的，补丁必须打在那里
    original = discovery.StructureGraphRegistry
    discovery.StructureGraphRegistry = lambda _s: _Boom()
    try:
        assert identify_node._graph_summary_block(agent) == ""
    finally:
        discovery.StructureGraphRegistry = original
    # 日志由内层的 build_identify_summary 记（graph.context），外层只是纵深防御，
    # 所以这里不断言 agent.logger 收到异常，只断言对外行为是空串。


# --- generate ------------------------------------------------------------- #


def _spec_ctx(settings):
    return SimpleNamespace(settings=settings)


def test_generate_prompt_carries_the_impact_surface(tmp_path):
    kb = _kb(tmp_path)
    sub = Subsystem(name="orderService", responsibility="orders", capabilities=["place order"])

    block = spec_steps._graph_impact_block(_spec_ctx(_settings(kb)), sub)

    assert block.startswith("\n\n代码结构图影响面:")
    assert "bean:orderService" in block


def test_generate_prompt_is_unchanged_without_a_snapshot(tmp_path):
    kb = _kb(tmp_path, with_snapshot=False)
    sub = Subsystem(name="orderService", responsibility="orders")

    assert spec_steps._graph_impact_block(_spec_ctx(_settings(kb)), sub) == ""


def test_generate_swallows_a_broken_registry(tmp_path):
    kb = _kb(tmp_path)
    sub = Subsystem(name="orderService", responsibility="orders")
    import porto_chatbot.graph.context as ctx_mod

    original = ctx_mod.build_subsystem_impact
    ctx_mod.build_subsystem_impact = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        assert spec_steps._graph_impact_block(_spec_ctx(_settings(kb)), sub) == ""
    finally:
        ctx_mod.build_subsystem_impact = original
