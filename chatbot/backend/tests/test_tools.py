from __future__ import annotations

import pytest

from porto_chatbot.models import SourceChunk, Subsystem
from porto_chatbot.tools import AgentToolContext, build_agent_tools
from porto_chatbot.vector_store import LocalVectorStore


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
