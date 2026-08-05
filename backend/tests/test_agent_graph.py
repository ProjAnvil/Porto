"""agent graph:state reducer + 拓扑 + interrupt + update_state。"""
from __future__ import annotations

import operator
import sqlite3
import threading
from typing import get_type_hints
from unittest.mock import MagicMock

from porto_chatbot.agent.graph import INTERRUPT_AFTER, STEPS, build_workflow_graph
from porto_chatbot.agent.nodes import retrieve as retrieve_node
from porto_chatbot.agent.nodes import understand as understand_node
from porto_chatbot.agent.state import PortoAgentState, _dict_merge
from porto_chatbot.llm import LLMClient
from porto_chatbot.models import SpecResult, Subsystem
from porto_chatbot.settings import Settings


def test_dict_merge_reducer():
    assert _dict_merge(None, {"a": 1}) == {"a": 1}
    assert _dict_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert _dict_merge({"a": 1, "x": 9}, {"a": 2}) == {"a": 2, "x": 9}  # 右覆盖 + 保旧


def test_state_reducer_annotations():
    """steps→operator.add(append);specs/spec_results→_dict_merge;current_step 存在。"""
    th = get_type_hints(PortoAgentState, include_extras=True)
    assert operator.add in th["steps"].__metadata__
    assert _dict_merge in th["specs"].__metadata__
    assert _dict_merge in th["spec_results"].__metadata__
    assert "current_step" in th


# ----------------------------- 节点签名 (state, *, config) ----------------------------- #


def _disabled_agent():  # llm.enabled=False → 走 fallback,不碰真 LLM/检索
    ag = MagicMock()
    ag.llm.enabled = False
    ag.logger.info = lambda *a, **k: None
    ag._step = lambda name, summary, data: {"steps": [{"name": name}]}
    return ag


def test_understand_node_reads_agent_from_config_and_returns_partial():
    state = {"workflow_id": "w", "project_name": "p", "prd_text": "需要一个订单管理模块", "steps": []}
    out = understand_node.understand_prd(state, config={"configurable": {"agent": _disabled_agent()}})
    # partial:不回传全量 state,只含改动键
    assert set(out).issubset({"understanding", "current_step", "steps"})
    assert out["current_step"] == "understand"
    assert out["understanding"]                      # fallback 产出非空


def test_retrieve_node_reads_agent_from_config():
    ag = _disabled_agent()
    ag.vector_store.ensure_index = lambda: None
    ag.vector_store.search = lambda q, top_k: []
    state = {"workflow_id": "w", "project_name": "p", "prd_text": "x", "top_k": 3, "steps": []}
    out = retrieve_node.retrieve_knowledge(state, config={"configurable": {"agent": ag}})
    assert out["current_step"] == "retrieve"
    assert out["sources"] == []


# ----------------------------- graph 拓扑 + interrupt_after ----------------------------- #


def _tmp_saver(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "g.sqlite3"), check_same_thread=False)
    from langgraph.checkpoint.sqlite import SqliteSaver
    sv = SqliteSaver(conn)
    sv.setup()
    return sv


def test_constants_match_design():
    assert STEPS == ["retrieve", "understand", "identify", "generate"]
    assert INTERRUPT_AFTER == ["understand", "identify", "generate"]


def test_graph_interrupt_points_with_standin_nodes(tmp_path, monkeypatch):
    """用 stand-in 节点替换真节点,验证拓扑 + interrupt_after 位置(不依赖 LLM/检索)。

    新拓扑:``identify --(dispatch_specs Send)--> generate(子图 fan-out) --> END``。
    generate 节点是 stand-in 子图(monkey-patch build_spec_subgraph);dispatch_specs
    用真的,验证 ``identify → generate`` 的 Send fan-out + interrupt 位置。
    """
    from langgraph.graph import END, START, StateGraph

    import porto_chatbot.agent.graph as graph_mod
    from porto_chatbot.specs.subgraph import SpecSubgraphState

    order: list[str] = []

    def mk(name):
        def fn(state, *, config):
            order.append(name)
            return {"current_step": name}
        return fn

    def identify_fn(state, *, config):
        order.append("identify")
        return {
            "current_step": "identify",
            "subsystems": [
                Subsystem(name="s1", responsibility="r1", capabilities=[], data_entities=[]),
                Subsystem(name="s2", responsibility="r2", capabilities=[], data_entities=[]),
            ],
        }

    def gen_node(state):
        # stand-in 子图节点:每个 Send 实例记一次 "generate"
        order.append("generate")
        sub_name = state["sub"].name
        return {
            "specs": {sub_name: "spec-text"},
            "spec_results": {sub_name: "stand-in"},
            "current_step": "generate",
        }

    def standin_subgraph():
        g = StateGraph(SpecSubgraphState)
        g.add_node("gen", gen_node)
        g.add_edge(START, "gen")
        g.add_edge("gen", END)
        return g.compile()

    monkeypatch.setattr(graph_mod.retrieve_node, "retrieve_knowledge", mk("retrieve"))
    monkeypatch.setattr(graph_mod.understand_node, "understand_prd", mk("understand"))
    monkeypatch.setattr(graph_mod.identify_node, "identify_subsystems", identify_fn)
    monkeypatch.setattr(graph_mod, "build_spec_subgraph", standin_subgraph)

    graph = build_workflow_graph(_tmp_saver(tmp_path))

    ag = MagicMock()
    ag.backend = ag.vector_store = ag.critic_llm = None
    ag.llm = MagicMock(enabled=False)
    ag.settings = MagicMock()
    ag._spec_sema = None
    ag.logger.info = lambda *a, **k: None

    cfg = {"configurable": {"thread_id": "w1", "agent": ag}}
    graph.invoke({"workflow_id": "w1"}, cfg)
    assert order == ["retrieve", "understand"]          # 停在 understand 后
    assert list(graph.get_state(cfg).next) == ["identify"]

    order.clear()
    list(graph.stream(None, cfg))
    assert order == ["identify"]                         # 推一格到 identify
    # identify 的 conditional 返回 2 个 Send → next 有 2 个 generate 实例
    assert list(graph.get_state(cfg).next) == ["generate", "generate"]

    order.clear()
    list(graph.stream(None, cfg))                        # generate fan-out(2 sub)→ interrupt
    assert order == ["generate", "generate"]             # 2 个子系统各一个 Send 实例
    assert list(graph.get_state(cfg).next) == []         # generate→END,interrupt 后 next 空


# ----------------------------- Task 9 I1: Send fan-out 端到端 ----------------------------- #


def test_send_fanout_merges_spec_results_e2e(tmp_path, monkeypatch):
    """I1:真 workflow graph Send fan-out → spec_results 有各子系统 key。

    锁定审计 B1:父图 ``_dict_merge`` reducer 合并各子图实例的 ``spec_results``,
    多子系统产物不被 LangGraph 静默丢弃。mock retrieve/understand/identify 为 stand-in
    (identify 产出 2 个 Subsystem),generate 用**真子图**(模板降级路径,无 LLM 调用),
    跑到 END 断言两个 key 都在 + specs 派生 + current_step。
    """
    import porto_chatbot.agent.graph as graph_mod

    settings = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        agent_provider="openai",
        agent_model="m",
    )
    settings.agent_api_key = None  # llm.enabled=False → 子图模板降级,不碰真 LLM
    settings.spec_refine_enabled = False
    llm = LLMClient(settings)

    ag = MagicMock()
    ag.settings = settings
    ag.llm = llm
    ag.backend = None
    ag.vector_store = None
    ag.critic_llm = llm
    ag._spec_sema = threading.Semaphore(4)
    ag.logger.info = lambda *a, **k: None
    ag.file_service = None

    subs = [
        Subsystem(name="auth", responsibility="认证", capabilities=["登录"], data_entities=["User"]),
        Subsystem(name="billing", responsibility="账单", capabilities=["出账"], data_entities=["Bill"]),
    ]

    monkeypatch.setattr(
        graph_mod.retrieve_node, "retrieve_knowledge", lambda s, *, config: {"current_step": "retrieve", "sources": []}
    )
    monkeypatch.setattr(
        graph_mod.understand_node, "understand_prd", lambda s, *, config: {"current_step": "understand", "understanding": "理解"}
    )
    monkeypatch.setattr(
        graph_mod.identify_node,
        "identify_subsystems",
        lambda s, *, config: {"current_step": "identify", "subsystems": subs},
    )
    # generate 用真 build_spec_subgraph() —— 不 patch

    graph = build_workflow_graph(_tmp_saver(tmp_path))
    cfg = {"configurable": {"thread_id": "w1", "agent": ag}}
    graph.invoke({"workflow_id": "w1", "project_name": "p", "prd_file_id": "f1"}, cfg)
    list(graph.stream(None, cfg))  # → identify(interrupt)
    list(graph.stream(None, cfg))  # → generate fan-out(2 实例)→ interrupt,spec_results 填充

    values = graph.get_state(cfg).values
    # B1 核心:两个子系统的 spec_results 都在(reducer 合并生效)
    assert set(values["spec_results"]) == {"auth", "billing"}
    for sr in values["spec_results"].values():
        assert isinstance(sr, SpecResult)
        assert sr.used_llm is False  # 模板降级
        assert sr.final  # 非空
    # specs 派生(供 workflow_store / PATCH /specs)+ current_step
    assert set(values["specs"]) == {"auth", "billing"}
    assert values["current_step"] == "generate"
