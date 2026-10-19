from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from porto_chatbot.models import SourceChunk, Subsystem
from porto_chatbot.settings import Settings
from porto_chatbot.tools import AgentToolContext, build_agent_tools
from porto_chatbot.vector_store import LocalVectorStore

_GRAPH_FIXTURE = Path(__file__).parent / "fixtures" / "structure.vg"


@pytest.fixture()
def graph_registry(tmp_path: Path, monkeypatch):
    """A real Tier-0 registry over a copy of the structure.vg fixture."""
    from porto_chatbot.graph import discovery
    from porto_chatbot.graph.discovery import (
        StructureGraphRegistry,
        reset_graph_caches,
    )

    # 宿主机器上若真装了 graph MCP，Tier 1 会抢在 Tier 0 前面并尝试起子进程。
    monkeypatch.setattr(discovery, "find_scv_graph_mcp", lambda: None)
    monkeypatch.setattr(discovery, "find_vectra_mcp", lambda: None)
    kb = tmp_path / "graph_kb"
    repo = kb / "alpha"
    repo.mkdir(parents=True)
    (repo / "structure.vg").write_bytes(_GRAPH_FIXTURE.read_bytes())
    settings = Settings(kb_dirs=[kb], graph_enabled=True, graph_mcp_enabled=False)
    yield StructureGraphRegistry(settings)
    reset_graph_caches()


@pytest.fixture()
def graph_ctx(graph_registry) -> AgentToolContext:
    return AgentToolContext(state={}, graph_registry=graph_registry)


@pytest.fixture()
def ctx(sample_settings) -> AgentToolContext:
    store = LocalVectorStore(sample_settings)
    store.build()
    return AgentToolContext(
        state={
            "prd_text": "# Demo\n需求：实现支付与风控。",
            "understanding": "支付+风控的业务理解",
            "subsystems": [
                Subsystem(
                    name="payment-service",
                    type="new",
                    responsibility="支付与退款",
                    capabilities=["支付发起", "退款"],
                    data_entities=["Payment", "Refund"],
                    dependencies=["risk-service"],
                ),
                Subsystem(name="risk-service", responsibility="风控"),
            ],
            "sources": [
                SourceChunk(id="1", path="a.md", title="a", text="payment channel routing", score=0.9),
                SourceChunk(id="2", path="b.md", title="b", text="order tracking", score=0.5),
            ],
        },
        vector_store=store,
    )


def _by_name(tools, name):
    return next(t for t in tools if t.name == name)


def test_build_agent_tools_schemas_valid(ctx):
    tools = build_agent_tools(ctx)
    names = [t.name for t in tools]
    assert "get_prd_text" in names
    assert "search_knowledgebase" in names
    for t in tools:
        assert t.description
        assert isinstance(t.input_schema, dict)
        assert t.input_schema.get("type") == "object"
        assert callable(t.handler)


def test_get_prd_text(ctx):
    tools = build_agent_tools(ctx)
    out = _by_name(tools, "get_prd_text").handler({})
    assert "支付" in out


def test_get_prd_text_missing(sample_settings):
    tools = build_agent_tools(AgentToolContext(state={}, vector_store=None))
    assert "尚未提供" in _by_name(tools, "get_prd_text").handler({})


def test_get_prd_text_truncates(sample_settings):
    tools = build_agent_tools(AgentToolContext(state={"prd_text": "x" * 10000}, vector_store=None))
    out = _by_name(tools, "get_prd_text").handler({})
    assert "已截断" in out


def test_get_understanding(ctx):
    tools = build_agent_tools(ctx)
    assert "业务理解" in _by_name(tools, "get_understanding").handler({})


def test_get_understanding_empty(sample_settings):
    tools = build_agent_tools(AgentToolContext(state={}, vector_store=None))
    assert "尚未生成" in _by_name(tools, "get_understanding").handler({})


def test_list_subsystems(ctx):
    tools = build_agent_tools(ctx)
    out = _by_name(tools, "list_subsystems").handler({})
    assert "payment-service" in out
    assert "risk-service" in out
    assert "2" in out


def test_list_subsystems_empty(sample_settings):
    tools = build_agent_tools(AgentToolContext(state={}, vector_store=None))
    assert "尚未生成" in _by_name(tools, "list_subsystems").handler({})


def test_get_subsystem_found(ctx):
    tools = build_agent_tools(ctx)
    out = _by_name(tools, "get_subsystem").handler({"name": "payment-service"})
    assert "Payment" in out  # data entity 出现
    assert "risk-service" in out  # dependency 出现


def test_get_subsystem_not_found(ctx):
    tools = build_agent_tools(ctx)
    out = _by_name(tools, "get_subsystem").handler({"name": "ghost"})
    assert "未找到" in out


def test_search_knowledgebase(ctx):
    tools = build_agent_tools(ctx)
    out = _by_name(tools, "search_knowledgebase").handler({"query": "payment"})
    assert "payment" in out.lower()
    # tool 检索结果应回填到 state.tool_sources
    assert len(ctx.state["tool_sources"]) >= 1


def test_search_knowledgebase_no_store(sample_settings):
    tools = build_agent_tools(AgentToolContext(state={}, vector_store=None))
    assert "不可用" in _by_name(tools, "search_knowledgebase").handler({"query": "x"})


def test_get_sources_returns_all(ctx):
    tools = build_agent_tools(ctx)
    out = _by_name(tools, "get_sources").handler({})
    assert "payment channel" in out
    assert "order tracking" in out


def test_get_sources_filters(ctx):
    tools = build_agent_tools(ctx)
    out = _by_name(tools, "get_sources").handler({"query": "payment"})
    assert "payment channel" in out


def test_get_sources_empty(sample_settings):
    tools = build_agent_tools(AgentToolContext(state={}, vector_store=None))
    assert "尚无" in _by_name(tools, "get_sources").handler({})


def test_get_sources_truncates_long(sample_settings):
    tools = build_agent_tools(AgentToolContext(
        state={"sources": [SourceChunk(id="1", path="a", title="a", text="Y" * 20000, score=1.0)]},
        vector_store=None,
    ))
    out = _by_name(tools, "get_sources").handler({})
    assert len(out) < 20000


# --- search_code_graph ---------------------------------------------------- #


def test_search_code_graph_absent_without_registry(sample_settings):
    """无 registry 时不注册该工具，免得 LLM 拿到一个只会说「不可用」的工具。"""
    names = [t.name for t in build_agent_tools(AgentToolContext(state={}))]
    assert "search_code_graph" not in names


def test_search_code_graph_registered_when_registry_present(graph_ctx):
    tools = build_agent_tools(graph_ctx)
    tool = _by_name(tools, "search_code_graph")
    assert tool.description
    assert tool.input_schema["required"] == ["query"]
    assert "top_k" in tool.input_schema["properties"]


def test_search_code_graph_returns_matching_nodes(graph_ctx):
    out = _by_name(build_agent_tools(graph_ctx), "search_code_graph").handler(
        {"query": "order", "top_k": 5}
    )
    assert "bean:orderService" in out
    assert "alpha" in out  # 仓库名出现在结果里


def test_search_code_graph_no_match(graph_ctx):
    out = _by_name(build_agent_tools(graph_ctx), "search_code_graph").handler(
        {"query": "zzzznotathing"}
    )
    assert "未找到" in out


def test_search_code_graph_fail_open_on_registry_error(graph_registry, monkeypatch):
    """graph_for 抛异常时必须降级成一段说明，绝不能让异常冒泡到 tool loop。"""

    class _BoomRegistry:
        def __init__(self, settings):
            self._settings = settings

        def graph_for(self, repo):
            raise RuntimeError("boom")

    boom = _BoomRegistry(graph_registry._settings)  # 测试替身直接借用真实 settings
    ctx = AgentToolContext(state={}, graph_registry=boom)
    out = _by_name(build_agent_tools(ctx), "search_code_graph").handler({"query": "order"})
    assert "失败" in out


def test_search_code_graph_skips_empty_graphs(tmp_path):
    """registry 存在但没有任何快照时，工具照常注册并返回说明。"""
    registry = MagicMock()
    registry._settings = Settings(kb_dirs=[tmp_path], graph_enabled=True)
    ctx = AgentToolContext(state={}, graph_registry=registry)
    out = _by_name(build_agent_tools(ctx), "search_code_graph").handler({"query": "order"})
    assert "不可用" in out
