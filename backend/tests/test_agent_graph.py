"""agent graph: state reducers + topology + interrupt + update_state."""
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
from porto_chatbot.models.enums import QueryTransformStrategy
from porto_chatbot.settings import Settings


def test_dict_merge_reducer():
    assert _dict_merge(None, {"a": 1}) == {"a": 1}
    assert _dict_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert _dict_merge({"a": 1, "x": 9}, {"a": 2}) == {"a": 2, "x": 9}  # the right side overrides and old keys are kept


def test_state_reducer_annotations():
    """steps → operator.add (append); specs / spec_results → _dict_merge; current_step exists."""
    th = get_type_hints(PortoAgentState, include_extras=True)
    assert operator.add in th["steps"].__metadata__
    assert _dict_merge in th["specs"].__metadata__
    assert _dict_merge in th["spec_results"].__metadata__
    assert "current_step" in th


# -------------------------- Node signature (state, *, config) -------------------------- #


def _disabled_agent():  # llm.enabled=False → takes the fallback, never touches the real LLM / retrieval
    ag = MagicMock()
    ag.llm.enabled = False
    ag.logger.info = lambda *a, **k: None
    ag._step = lambda name, summary, data: {"steps": [{"name": name}]}
    # Task 8: retrieve_knowledge reads settings fields through retrieve_with_transform;
    # a MagicMock auto-attribute returns a MagicMock instead of an enum, so strategy != NONE
    # would take the LLM branch by mistake. Set NONE + rerank off explicitly so the original
    # store.search path is used.
    ag.settings.workflow_query_transform_strategy = QueryTransformStrategy.NONE
    ag.settings.rerank_enabled = False
    ag.settings.multi_query_count = 4
    return ag


def test_understand_node_reads_agent_from_config_and_returns_partial():
    state = {"workflow_id": "w", "project_name": "p", "prd_text": "需要一个订单管理模块", "steps": []}
    out = understand_node.understand_prd(state, config={"configurable": {"agent": _disabled_agent()}})
    # partial: does not return the full state, only the changed keys
    assert set(out).issubset({"understanding", "current_step", "steps"})
    assert out["current_step"] == "understand"
    assert out["understanding"]                      # the fallback output is non-empty


def test_retrieve_node_reads_agent_from_config():
    ag = _disabled_agent()
    ag.vector_store.ensure_index = lambda: None
    ag.vector_store.search = lambda q, top_k: []
    state = {"workflow_id": "w", "project_name": "p", "prd_text": "x", "top_k": 3, "steps": []}
    out = retrieve_node.retrieve_knowledge(state, config={"configurable": {"agent": ag}})
    assert out["current_step"] == "retrieve"
    assert out["sources"] == []


# -------------------------- Graph topology + interrupt_after -------------------------- #


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
    """Replace the real nodes with stand-ins to verify the topology and interrupt_after
    positions (no LLM / retrieval needed).

    New topology: ``identify --(dispatch_specs Send)--> generate (subgraph fan-out) -->
    END``. The generate node is a stand-in subgraph (build_spec_subgraph is monkey-patched);
    dispatch_specs is the real one, so the Send fan-out ``identify → generate`` and the
    interrupt positions are verified.
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
        # Stand-in subgraph node: records one "generate" per Send instance
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
    assert order == ["retrieve", "understand"]          # stops after understand
    assert list(graph.get_state(cfg).next) == ["identify"]

    order.clear()
    list(graph.stream(None, cfg))
    assert order == ["identify"]                         # advances one step to identify
    # The conditional of identify returns 2 Sends → next has 2 generate instances
    assert list(graph.get_state(cfg).next) == ["generate", "generate"]

    order.clear()
    list(graph.stream(None, cfg))                        # generate fan-out(2 sub)→ interrupt
    assert order == ["generate", "generate"]             # one Send instance per subsystem, 2 in total
    assert list(graph.get_state(cfg).next) == []         # generate → END, next is empty after the interrupt


# ------------------------- Task 9 I1: Send fan-out end to end ------------------------- #


def test_send_fanout_merges_spec_results_e2e(tmp_path, monkeypatch):
    """I1: the real workflow graph Send fan-out → spec_results has a key per subsystem.

    Locks in audit item B1: the parent graph's ``_dict_merge`` reducer merges the
    ``spec_results`` of each subgraph instance, so the output of multiple subsystems is not
    silently dropped by LangGraph. retrieve / understand / identify are mocked with stand-ins
    (identify produces 2 Subsystems) and generate uses the **real subgraph** (template fallback
    path, no LLM call); run to END and assert both keys are present + specs are derived +
    current_step.
    """
    import porto_chatbot.agent.graph as graph_mod

    settings = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        agent_provider="openai",
        agent_model="m",
    )
    settings.agent_api_key = None  # llm.enabled=False → the subgraph takes the template fallback, no real LLM
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
    # generate uses the real build_spec_subgraph(): not patched

    graph = build_workflow_graph(_tmp_saver(tmp_path))
    cfg = {"configurable": {"thread_id": "w1", "agent": ag}}
    graph.invoke({"workflow_id": "w1", "project_name": "p", "prd_file_id": "f1"}, cfg)
    list(graph.stream(None, cfg))  # → identify(interrupt)
    list(graph.stream(None, cfg))  # → generate fan-out (2 instances) → interrupt, spec_results filled

    values = graph.get_state(cfg).values
    # Core of B1: the spec_results of both subsystems are present (the reducer merge works)
    assert set(values["spec_results"]) == {"auth", "billing"}
    for sr in values["spec_results"].values():
        assert isinstance(sr, SpecResult)
        assert sr.used_llm is False  # template fallback
        assert sr.final  # non-empty
    # specs are derived (for workflow_store / PATCH /specs) + current_step
    assert set(values["specs"]) == {"auth", "billing"}
    assert values["current_step"] == "generate"


def test_send_fanout_llm_enabled_e2e(tmp_path, monkeypatch):
    """I1 (verifies the C1 fix): end-to-end Send fan-out on the mocked LLM-enabled path.

    C1 bug: ``init_spec`` injects ctx_* into its local state from
    ``config["configurable"]["agent"]``, but its return value does not include ctx_* → the
    state seen by critique / refine / finalize has ``ctx_llm=None`` → ``_llm_active`` returns
    False → critique returns ``{}`` outright → the whole stop-condition loop is skipped → the
    spec degrades to the first round (``used_llm=False``, ``iterations=0``, ``attempts=[]``).

    This test mocks the three ``LLMClient`` entry points ``complete_with_tools`` /
    ``complete_structured`` / ``complete`` (so generate_initial_spec / critique_spec /
    refine_spec go through the real LangchainBackend) and lets the LLM-enabled path really
    run. It asserts ``SpecResult.used_llm is True``, ``iterations >= 1`` and a non-empty
    ``attempts``, which only hold once C1 is fixed (ctx_* returned).
    """
    import porto_chatbot.agent.graph as graph_mod
    from porto_chatbot.agent.backends import LangchainBackend
    from porto_chatbot.llm.types import ToolLoopResult

    settings = Settings(
        kb_dirs=[tmp_path / "kb"],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        agent_provider="openai",
        agent_model="gpt-4o",
    )
    settings.agent_api_key = "sk-mock"  # → llm.enabled = True (_build_client is not None)
    settings.spec_refine_enabled = True  # one of the two switches behind _llm_active
    settings.spec_refine_max_iter = 3
    settings.spec_refine_pass_score = 10  # the mock score of 5 does not pass → the refine loop runs

    llm = LLMClient(settings)

    # Mock the three LLM entry points to bypass the real LLM / network
    llm.complete_with_tools = lambda system, user, tools, *, messages=None, max_turns=None: ToolLoopResult(
        text=f"# Mock initial spec\n\n{user}",
        tool_calls=[],
        turns=1,
        truncated=False,
        reason=None,
    )
    llm.complete_structured = lambda system, user, schema_hint, *, messages=None: {
        "verdict": "needs_improvement",
        "score": 5,  # < pass_score (10) and <= the previous best → the regression stop fires on round 2
        "feedback": "需要补充 API 错误码与数据模型字段",
        "per_dimension": {},
    }
    llm.complete = lambda system, user, *, messages=None: "# Mock refined spec\n\n改进后的规格"

    ag = MagicMock()
    ag.settings = settings
    ag.llm = llm
    ag.backend = LangchainBackend(llm)  # real backend; the mock sits one level down, at LLMClient
    ag.vector_store = None
    ag.critic_llm = llm
    ag._spec_sema = threading.Semaphore(4)
    ag.logger.info = lambda *a, **k: None
    ag.file_service = None

    subs = [
        Subsystem(name="auth", responsibility="认证", capabilities=["登录"], data_entities=["User"]),
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
    # generate uses the real build_spec_subgraph(): not patched

    graph = build_workflow_graph(_tmp_saver(tmp_path))
    cfg = {"configurable": {"thread_id": "w1", "agent": ag}}
    graph.invoke({"workflow_id": "w1", "project_name": "p", "prd_file_id": "f1"}, cfg)
    list(graph.stream(None, cfg))  # → identify(interrupt)
    list(graph.stream(None, cfg))  # → generate fan-out → interrupt, spec_results filled

    values = graph.get_state(cfg).values
    assert "auth" in values["spec_results"]
    sr = values["spec_results"]["auth"]
    assert isinstance(sr, SpecResult)
    # Core of C1: on the LLM-enabled path the spec really went through the critique / refine
    # loop (it did not degrade to the first round)
    assert sr.used_llm is True
    assert sr.iterations >= 1
    assert sr.attempts  # non-empty
    assert values["current_step"] == "generate"
