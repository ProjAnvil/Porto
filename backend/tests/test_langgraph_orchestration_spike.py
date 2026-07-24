"""L2 spike: langgraph 编排原语(interrupt / resume / update_state rewind / config 注入 / Pydantic 往返 / 并发)。

结论见 plan §Spike Conclusions。这些行为是 Task 3–5(graph + executor)的实现依据。
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Annotated, TypedDict

import operator
from pydantic import BaseModel
from langgraph.graph import START, END, StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver


def _dict_merge(left: dict, right: dict) -> dict:
    return {**(left or {}), **(right or {})}


class _Widget(BaseModel):
    name: str
    qty: int


class _S(TypedDict, total=False):
    widgets: Annotated[list[_Widget], operator.add]
    specs: Annotated[dict, _dict_merge]
    current_step: str
    seen_agent: str


def _saver(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "spike.sqlite3"), check_same_thread=False)
    sv = SqliteSaver(conn)
    sv.setup()
    return sv


# ---- ① interrupt_after 暂停 + stream(None) 续跑 + ② Pydantic 过 checkpoint 往返 ----
def test_interrupt_then_resume_and_pydantic_roundtrip(tmp_path):
    consumed = {}

    def node_a(state):
        # append a Pydantic model (跨 checkpoint 后须仍是 _Widget,不能降级成 dict)
        return {"widgets": [_Widget(name="a", qty=1)], "current_step": "a"}

    def node_b(state):
        w = state["widgets"][0]
        consumed["type"] = type(w).__name__
        consumed["name"] = w.name  # 属性访问 —— 仅当重建为 _Widget 才成立
        return {"specs": {"x": "b"}, "current_step": "b"}

    g = StateGraph(_S)
    g.add_node("a", node_a)
    g.add_node("b", node_b)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    graph = g.compile(checkpointer=_saver(tmp_path), interrupt_after=["a"])
    cfg = {"configurable": {"thread_id": "t1"}}

    graph.invoke({"widgets": [], "specs": {}}, cfg)
    st = graph.get_state(cfg)
    assert list(st.next) == ["b"]                      # 暂停在 a 之后
    assert st.values["current_step"] == "a"
    assert [type(w).__name__ for w in st.values["widgets"]] == ["_Widget"]

    chunks = list(graph.stream(None, cfg))             # 续跑 a 之后 → b → END
    st2 = graph.get_state(cfg)
    assert list(st2.next) == []                        # 到 END
    assert consumed["type"] == "_Widget"               # Pydantic 过 checkpoint 往返成功
    assert consumed["name"] == "a"
    assert st2.values["specs"] == {"x": "b"}


# ---- ③ update_state(as_node=) 回退并重算下游 ----
def test_update_state_rewinds_and_reruns_downstream(tmp_path):
    order: list[str] = []

    def mk(name):
        def fn(state):
            order.append(name)
            return {"current_step": name}
        return fn

    g = StateGraph(_S)
    for n in ("a", "b", "c"):
        g.add_node(n, mk(n))
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", END)
    graph = g.compile(checkpointer=_saver(tmp_path), interrupt_after=["a", "b"])
    cfg = {"configurable": {"thread_id": "t2"}}

    graph.invoke({}, cfg)                              # 跑 a,停在 a 后
    assert order == ["a"]
    list(graph.stream(None, cfg))                      # 跑 b,停在 b 后
    assert order == ["a", "b"]

    graph.update_state(cfg, {"current_step": "a"}, as_node="a")  # 回退到 a 之后
    assert list(graph.get_state(cfg).next) == ["b"]
    order.clear()
    list(graph.stream(None, cfg))                      # 必须重跑 b(下游重算)
    assert order == ["b"], f"回退后应重跑 b,实际 {order}"


# ---- ④ configurable 注入 agent(节点从 config 取对象)----
def test_configurable_agent_injection(tmp_path):
    def node(state, *, config):                        # langgraph 1.x: (state, config)
        return {"seen_agent": config["configurable"]["agent"]}

    g = StateGraph(_S)
    g.add_node("n", node)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    graph = g.compile(checkpointer=_saver(tmp_path))
    cfg = {"configurable": {"thread_id": "t3", "agent": "AGENT_OBJ"}}
    graph.invoke({}, cfg)
    assert graph.get_state(cfg).values["seen_agent"] == "AGENT_OBJ"


# ---- ⑤ 多 workflow(不同 thread_id)在共享 SqliteSaver 上并发不冲突 ----
def test_shared_saver_concurrent_threads(tmp_path):
    def node(state):
        return {"specs": {"k": "v"}}
    g = StateGraph(_S)
    g.add_node("n", node)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    saver = _saver(tmp_path)                           # 单 saver,多 thread_id
    graph = g.compile(checkpointer=saver)

    errors: list[BaseException] = []

    def run(tid):
        try:
            graph.invoke({}, {"configurable": {"thread_id": tid}})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"t{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], f"共享 saver 并发出错: {errors}"
