"""agent graph:state reducer + 拓扑 + interrupt + update_state。"""
from __future__ import annotations

import operator
import sqlite3
from typing import get_type_hints
from unittest.mock import MagicMock

from porto_chatbot.agent.graph import INTERRUPT_AFTER, STEPS, build_workflow_graph
from porto_chatbot.agent.nodes import retrieve as retrieve_node
from porto_chatbot.agent.nodes import understand as understand_node
from porto_chatbot.agent.state import PortoAgentState, _dict_merge


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
    assert STEPS == ["retrieve", "understand", "identify", "generate", "evaluate"]
    assert INTERRUPT_AFTER == ["understand", "identify", "generate"]


def test_graph_interrupt_points_with_standin_nodes(tmp_path, monkeypatch):
    """用 stand-in 节点替换真节点,验证拓扑 + interrupt_after 位置(不依赖 LLM/检索)。"""
    import porto_chatbot.agent.graph as graph_mod

    order: list[str] = []

    def mk(name):
        def fn(state, *, config):
            order.append(name)
            return {"current_step": name}
        return fn

    # 临时把 _NODE_FNS 指向 stand-in(只测拓扑,不跑真节点)
    saved = dict(graph_mod._NODE_FNS)
    for n in STEPS:
        graph_mod._NODE_FNS[n] = mk(n)
    try:
        graph = build_workflow_graph(_tmp_saver(tmp_path))
    finally:
        graph_mod._NODE_FNS.clear()
        graph_mod._NODE_FNS.update(saved)

    cfg = {"configurable": {"thread_id": "w1", "agent": None}}
    graph.invoke({}, cfg)
    assert order == ["retrieve", "understand"]          # 停在 understand 后
    assert list(graph.get_state(cfg).next) == ["identify"]

    order.clear()
    list(graph.stream(None, cfg))
    assert order == ["identify"]                         # 推一格到 identify
    assert list(graph.get_state(cfg).next) == ["generate"]

    order.clear()
    list(graph.stream(None, cfg))                        # generate 后停
    assert order == ["generate"]
    order.clear()
    list(graph.stream(None, cfg))                        # evaluate → END
    assert order == ["evaluate"]
    assert list(graph.get_state(cfg).next) == []
