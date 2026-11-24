"""WorkflowExecutor: a background thread drives the langgraph graph, with a per-workflow
lock that prevents concurrent advance.

The tests inject a trivial graph (stand-in nodes + a temporary SqliteSaver) into the
executor and verify start / advance / failed / the concurrency guard / projection keeping
produced_by. No real node logic runs.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest
from langgraph.graph import END, START, StateGraph

from porto_chatbot.agent.graph import STEPS
from porto_chatbot.settings import Settings
from porto_chatbot.workflow_executor import WorkflowExecutor, WorkflowRunning
from porto_chatbot.workflow_store import WorkflowStore


def _saver(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "ex.sqlite3"), check_same_thread=False)
    from langgraph.checkpoint.sqlite import SqliteSaver

    sv = SqliteSaver(conn)
    sv.setup()
    return sv


#: Output written by the stand-in node for each step (note that specs / spec_results must be
#: dicts: on PortoAgentState they carry a dict-merge reducer, and a string would crash it).
_OUT_VALS = {
    "retrieve": {"sources": "retrieve-val"},
    "understand": {"understanding": "understand-val"},
    "identify": {"subsystems": "identify-val"},
    "generate": {"specs": {"default": "generate-val"}, "spec_results": {"default": "gen"}},
}


def _trivial_graph(tmp_path, *, fail_on=None, slow_step=None, slow_event=None):
    """A stand-in graph with the same topology and interrupt_after points as the real one; each
    node writes its output key + current_step.

    fail_on: raise when this step is reached (exercises the failed path).
    slow_step + slow_event: only when slow_step is reached, set (enter) and block on
    release.wait (exercises the guard: the worker keeps holding it inside that node; other
    nodes do not block).
    """
    from porto_chatbot.agent.state import PortoAgentState

    def mk(name):
        def fn(state, *, config):
            if slow_event and name == slow_step:
                enter, release = slow_event
                enter.set()
                release.wait(timeout=5.0)
            if fail_on and name == fail_on:
                raise RuntimeError("boom")
            return {**_OUT_VALS[name], "current_step": name}

        return fn

    g = StateGraph(PortoAgentState)
    for n in STEPS:
        g.add_node(n, mk(n))
    g.add_edge(START, STEPS[0])
    for a, b in zip(STEPS, STEPS[1:], strict=False):
        g.add_edge(a, b)
    g.add_edge(STEPS[-1], END)
    return g.compile(
        checkpointer=_saver(tmp_path), interrupt_after=["understand", "identify", "generate"]
    )


def _make(tmp_path, **kw):
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    ex = WorkflowExecutor(settings, store, _trivial_graph(tmp_path, **kw))
    return ex, store


def _create(store):
    return store.create(
        "s", "p", "prd", 6, {"embedding_provider": "local"}, {"agent_provider": "openai"}
    )


def test_start_runs_to_understand_checkpoint(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    row = store.get(wid)
    assert row["status"] == "awaiting_input"
    assert row["current_step"] == "understand"
    outs = store.get_outputs(wid)
    assert outs["retrieve"]["output"]["sources"] == "retrieve-val"
    assert outs["understand"]["output"]["understanding"] == "understand-val"


def test_advance_runs_next_checkpoint(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    assert ex.advance(wid) is True
    ex.wait(wid, timeout=5)
    assert store.get(wid)["current_step"] == "identify"


def test_advance_to_completed(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    for _ in range(3):  # understand → identify → generate → END (generate is the last step)
        assert ex.advance(wid) is True
        ex.wait(wid, timeout=5)
    assert store.get(wid)["status"] == "completed"
    assert store.get(wid)["current_step"] == "generate"


def test_advance_returns_false_when_running(tmp_path):
    """During advance the worker enters the slow node (identify) and keeps holding the guard:
    another advance must return False."""
    enter, release = threading.Event(), threading.Event()
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    ex = WorkflowExecutor(
        settings,
        store,
        _trivial_graph(tmp_path, slow_step="identify", slow_event=(enter, release)),
    )
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)  # stops at understand; identify is not reached, so nothing blocks
    assert ex.advance(wid) is True  # the worker enters identify → blocks, holding the guard
    assert enter.wait(timeout=2.0)
    assert ex.advance(wid) is False  # the guard is held → the 409 case
    release.set()
    ex.wait(wid, timeout=5)


def test_failed_records_error(tmp_path):
    ex, store = _make(tmp_path, fail_on="understand")
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    row = store.get(wid)
    assert row["status"] == "failed"
    assert "boom" in row["error"]


def test_persist_preserves_user_produced_by(tmp_path):
    """After advance, earlier (user-edited) steps keep produced_by=user; new steps are ai.

    Simulate understand already being produced_by=user (written straight into the store),
    then advance runs identify. Projection: the understand content comes from the graph state
    (the stand-in's "understand-val", which differs from the store's "user-edited" → treated as
    changed → rewritten, but produced_by keeps the existing "user"); identify is a new step, so
    ai.
    """
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)  # stops at understand
    store.save_output(wid, "understand", {"understanding": "user-edited"}, "user")

    assert ex.advance(wid) is True
    ex.wait(wid, timeout=5)  # → identify

    outs = store.get_outputs(wid)
    assert outs["understand"]["produced_by"] == "user"  # not overwritten with ai
    assert outs["identify"]["produced_by"] == "ai"


def test_two_rapid_advances_first_wins(tmp_path):
    """Regression: two rapid advances on the same workflow, the first True (advance takes the
    guard itself and starts the worker), the second False (the guard is held by the worker).
    Single graph with slow_step=identify; neither the graph nor the saver is swapped."""
    enter, release = threading.Event(), threading.Event()
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    ex = WorkflowExecutor(
        settings,
        store,
        _trivial_graph(tmp_path, slow_step="identify", slow_event=(enter, release)),
    )
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)  # stops at understand, the guard is free

    r1 = ex.advance(wid)
    assert r1 is True  # advance takes the guard itself and starts the worker
    assert enter.wait(timeout=2.0)  # the worker has entered identify and holds the guard
    r2 = ex.advance(wid)
    assert r2 is False  # the guard is held
    release.set()
    ex.wait(wid, timeout=5)
    assert store.get(wid)["current_step"] == "identify"


# ---------------------------------------------------------------- Task 5: PUT / PATCH / recover


def test_update_step_rewinds_and_marks_user(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)  # understand
    ex.wait(wid, timeout=5)
    ex.advance(wid)  # identify
    ex.wait(wid, timeout=5)
    assert "identify" in store.get_outputs(wid)

    ex.update_step(wid, "understand", {"understanding": "edited"})
    row = store.get(wid)
    assert row["current_step"] == "understand"
    assert row["status"] == "awaiting_input"
    outs = store.get_outputs(wid)
    assert outs["understand"]["produced_by"] == "user"
    assert outs["understand"]["output"]["understanding"] == "edited"
    assert "identify" not in outs  # downstream cleared


def test_update_step_then_advance_preserves_user_edit(tmp_path):
    """Important #1: after a PUT edit and an advance, the user's edit must survive (rather than
    being overwritten by the node's original output).

    Mechanism: the graph.update_state(as_node=understand) in update_step writes the edited
    content into the graph checkpoint; on advance the understand node does not run again (we
    are already downstream of it) and _project_state sees the graph-state understanding ==
    the store's edited value → skip-if-equal → preserved.
    Assertion: after advancing to identify, understand.output.understanding in the store is still
    the user's edited value.
    """
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)  # stops at understand (the original "understand-val")
    ex.wait(wid, timeout=5)
    ex.advance(wid)  # → identify (the original "identify-val")
    ex.wait(wid, timeout=5)

    # The user edits understand (overwriting the original "understand-val" → "user-edited")
    ex.update_step(wid, "understand", {"understanding": "user-edited"})
    outs = store.get_outputs(wid)
    assert outs["understand"]["output"]["understanding"] == "user-edited"
    assert outs["understand"]["produced_by"] == "user"

    # advance: the identify node runs again (update_step cleared the downstream); understand
    # does not run again
    assert ex.advance(wid) is True
    ex.wait(wid, timeout=5)

    # Key assertion: the understand content is still the user's edited value (not overwritten
    # by the node's original "understand-val")
    outs = store.get_outputs(wid)
    assert outs["understand"]["output"]["understanding"] == "user-edited", (
        "user-edited content lost after advance!"
    )
    assert outs["understand"]["produced_by"] == "user"  # the audit field is kept too
    assert "identify" in outs  # identify is back after rerunning


def test_update_spec_updates_store_and_graph(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    # Run to the generate checkpoint (in the trivial graph generate writes specs={"default": ...})
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    for _ in range(2):  # → identify → generate
        ex.advance(wid)
        ex.wait(wid, timeout=5)
    assert store.get(wid)["current_step"] == "generate"

    ok = ex.update_spec(wid, "default", "new body")  # changes the "default" key in specs
    assert ok is True
    outs = store.get_outputs(wid)
    assert outs["generate"]["produced_by"] == "ai"  # the audit field is untouched
    assert outs["generate"]["output"]["specs"]["default"] == "new body"
    # The graph state has been dict-merged (specs is a dict-merge reducer)
    cfg = {"configurable": {"thread_id": wid}}
    assert ex.graph.get_state(cfg).values["specs"]["default"] == "new body"


def test_update_spec_missing_returns_false(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    assert ex.update_spec(wid, "nope", "x") is False  # no generate output


def test_recover_at_interrupt_marks_awaiting(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)  # understand (the checkpoint exists)
    ex.wait(wid, timeout=5)
    store.update_status(wid, "running")  # simulate a crash leaving status=running
    n = ex.recover_on_startup()
    assert n == 1
    assert store.get(wid)["status"] == "awaiting_input"  # a checkpoint sits at the interrupt → resumable


def test_recover_no_checkpoint_marks_interrupted(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    store.update_status(wid, "running", current_step="understand")  # the graph never ran
    n = ex.recover_on_startup()
    assert n == 1
    row = store.get(wid)
    assert row["status"] == "interrupted"
    assert row["current_step"] == "understand"  # no checkpoint → keeps the existing value


def test_recover_awaiting_input_without_checkpoint_marks_interrupted(tmp_path):
    """Important #1: an awaiting_input orphan left behind pre-L2 has no checkpoint → marked
    interrupted.

    Otherwise the user's first /advance makes graph.stream(None, config) receive
    "Received no input for __start__" → caught by the worker → status=failed, a poor
    experience.
    """
    ex, store = _make(tmp_path)
    wid = _create(store)
    store.update_status(wid, "awaiting_input", current_step="understand")  # no checkpoint
    n = ex.recover_on_startup()
    assert n == 1
    row = store.get(wid)
    assert row["status"] == "interrupted"  # tells the user it cannot simply be resumed
    assert row["current_step"] == "understand"  # keeps the existing value


def test_recover_awaiting_input_with_checkpoint_left_untouched(tmp_path):
    """A normal L2 awaiting_input (checkpoint at the interrupt) is left alone by recovery.

    Regression: recover must not mislabel an L2 workflow that is halfway through and paused
    at the understand interrupt.
    """
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)  # stops at understand, the checkpoint exists
    ex.wait(wid, timeout=5)
    assert store.get(wid)["status"] == "awaiting_input"

    n = ex.recover_on_startup()
    assert n == 1
    row = store.get(wid)
    assert row["status"] == "awaiting_input"  # not touched
    assert row["current_step"] == "understand"


def test_recover_completed_graph_marks_completed(tmp_path):
    """Minor #2: the checkpoint has reached END (next is empty) but the status was reset to
    running → recover marks it completed.

    Scenario: the worker crashes between the return of graph.stream and _sync_status, the
    checkpoint shows the graph has finished, but the status in the database is still running.
    recover should recognize this as completed (not interrupted).
    """
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    for _ in range(3):  # understand → identify → generate → END (generate is the last step)
        assert ex.advance(wid) is True
        ex.wait(wid, timeout=5)
    assert store.get(wid)["status"] == "completed"

    # Simulate the worker crashing before sync_status: the status goes back to running, but
    # the checkpoint has already reached END
    store.update_status(wid, "running")
    n = ex.recover_on_startup()
    assert n == 1
    row = store.get(wid)
    assert row["status"] == "completed"
    assert row["current_step"] == "generate"


def test_step_output_keys_match_steps():
    """F2: the keys of _STEP_OUTPUT_KEYS match agent.graph.STEPS, the single source of truth
    (guards against drift).

    The module-level assert in the executor protects this always-on as well; the test
    records the intent explicitly.
    """
    from porto_chatbot.workflow_executor import _STEP_OUTPUT_KEYS

    assert list(_STEP_OUTPUT_KEYS.keys()) == STEPS


# ---------------------------------------------------------------- F3: PUT/PATCH concurrency guard


def test_update_step_rejects_while_running(tmp_path):
    """F3: while an advance is in progress (the worker holds the guard), update_step must raise
    WorkflowRunning (the route maps it to 409) instead of silently racing graph.stream and
    losing the update (the spike confirmed that the PUT's update_state gets overwritten by the
    checkpoint write when the worker finishes identify, and the edited content is lost
    entirely)."""
    enter, release = threading.Event(), threading.Event()
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    ex = WorkflowExecutor(
        settings,
        store,
        _trivial_graph(tmp_path, slow_step="identify", slow_event=(enter, release)),
    )
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)  # stops at understand
    ex.advance(wid)  # the worker enters identify (slow, holding the guard)
    assert enter.wait(timeout=2)

    with pytest.raises(WorkflowRunning):
        ex.update_step(wid, "understand", {"understanding": "edited-during-advance"})

    release.set()
    ex.wait(wid, timeout=5)


def test_update_spec_rejects_while_running(tmp_path):
    """F3: while an advance is in progress, update_spec must likewise raise WorkflowRunning (the
    route maps it to 409). acquire happens before the store lookup, so the request is rejected
    as running even when the spec does not exist."""
    enter, release = threading.Event(), threading.Event()
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    ex = WorkflowExecutor(
        settings,
        store,
        _trivial_graph(tmp_path, slow_step="identify", slow_event=(enter, release)),
    )
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    ex.advance(wid)
    assert enter.wait(timeout=2)

    with pytest.raises(WorkflowRunning):
        ex.update_spec(wid, "any", "body")

    release.set()
    ex.wait(wid, timeout=5)


# ---------------------------------------------------------------- Task 6: tool_meta projection


def test_project_state_attaches_tool_meta_single_step(tmp_path):
    from unittest.mock import MagicMock

    from porto_chatbot.models import AgentStep

    store = WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))
    wid = store.create("s", "p", "prd", 6, {}, {"agent_max_tool_turns": 10})
    executor = WorkflowExecutor(
        Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"), store, graph=MagicMock()
    )
    snap = MagicMock()
    snap.values = {
        "understanding": "报告",
        "steps": [
            AgentStep(
                name="understand_prd",
                status="completed",
                data={"tool_meta": {"turns": 4, "truncated": True, "max_turns": 10}},
            )
        ],
        "spec_results": {},
    }
    snap.next = ("identify",)
    executor.graph.get_state = MagicMock(return_value=snap)
    executor._project_state(wid, {"configurable": {"thread_id": wid}})
    out = store.get_outputs(wid)["understand"]["output"]
    assert out["understanding"] == "报告"
    assert out["tool_meta"]["truncated"] is True
    assert out["tool_meta"]["turns"] == 4


def test_project_state_attaches_tool_meta_generate(tmp_path):
    from unittest.mock import MagicMock

    store = WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))
    wid = store.create("s", "p", "prd", 6, {}, {"agent_max_tool_turns": 10})
    executor = WorkflowExecutor(
        Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"), store, graph=MagicMock()
    )
    snap = MagicMock()
    snap.values = {
        "specs": {"wallet": "s1", "proc": "s2"},
        "spec_results": {
            "wallet": {"final": "s1", "tool_meta": {"truncated": True}},
            "proc": {"final": "s2", "tool_meta": {"truncated": False}},
        },
        "steps": [],
    }
    snap.next = ()  # generate is the last step; once done, next is empty (= END)
    executor.graph.get_state = MagicMock(return_value=snap)
    executor._project_state(wid, {"configurable": {"thread_id": wid}})
    out = store.get_outputs(wid)["generate"]["output"]
    assert out["tool_meta"]["truncated"] is True  # any


def test_project_state_tool_meta_isolated_per_step(tmp_path):
    """Cross-step isolation: when state.steps contains both understand_prd (with tool_meta) and
    identify_subsystems (without tool_meta), understand is projected into tool_meta and
    identify must not be given understand's tool_meta by mistake (regression: _tool_meta_for
    once lacked the name filter).
    """
    from unittest.mock import MagicMock

    from porto_chatbot.models import AgentStep

    store = WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))
    wid = store.create("s", "p", "prd", 6, {}, {"agent_max_tool_turns": 10})
    executor = WorkflowExecutor(
        Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"), store, graph=MagicMock()
    )
    snap = MagicMock()
    snap.values = {
        "understanding": "understand 报告",
        "subsystems": [{"name": "wallet"}],
        "steps": [
            AgentStep(
                name="understand_prd",
                status="completed",
                data={"tool_meta": {"turns": 4, "truncated": True, "max_turns": 10}},
            ),
            AgentStep(
                name="identify_subsystems",
                status="completed",
                data={"subsystems": [{"name": "wallet"}]},  # no tool_meta
            ),
        ],
        "spec_results": {},
    }
    snap.next = ("generate",)  # → completed = ["retrieve", "understand", "identify"]
    executor.graph.get_state = MagicMock(return_value=snap)
    executor._project_state(wid, {"configurable": {"thread_id": wid}})
    outs = store.get_outputs(wid)
    # understand gets its own tool_meta
    understand_out = outs["understand"]["output"]
    assert understand_out["tool_meta"]["truncated"] is True
    assert understand_out["tool_meta"]["turns"] == 4
    # identify must not be given understand's tool_meta by mistake
    identify_out = outs["identify"]["output"]
    assert "tool_meta" not in identify_out


# ---------------------------------------------------------------- Task 7: rerun_step


def test_rerun_step_scales_max_turns_and_persists(tmp_path):
    store = WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))
    wid = store.create("s", "p", "prd", 6, {}, {"agent_max_tool_turns": 10})
    # Rebuilding the trivial graph + the real agent is driven by the snapshot; here the new_max
    # algorithm + persistence are verified directly
    import json
    from unittest.mock import MagicMock
    executor = WorkflowExecutor(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"), store, graph=MagicMock())
    new_max = executor._next_max_turns(wid)  # ceil(10*1.5)=15
    assert new_max == 15
    executor._apply_new_max(wid, new_max)
    snap = json.loads(store.get(wid)["agent_snapshot"])
    assert snap["agent_max_tool_turns"] == 15


def test_rerun_step_caps_at_hard_cap(tmp_path):
    from unittest.mock import MagicMock
    store = WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))
    wid = store.create("s", "p", "prd", 6, {}, {"agent_max_tool_turns": 30})
    executor = WorkflowExecutor(
        Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", tool_turn_hard_cap=40), store, graph=MagicMock())
    assert executor._next_max_turns(wid) == 40  # ceil(30*1.5)=45 → cap 40


def test_rerun_step_at_cap_raises(tmp_path):
    from unittest.mock import MagicMock
    store = WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))
    wid = store.create("s", "p", "prd", 6, {}, {"agent_max_tool_turns": 40})
    executor = WorkflowExecutor(
        Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", tool_turn_hard_cap=40), store, graph=MagicMock())
    import pytest
    with pytest.raises(WorkflowRunning):
        executor.rerun_step(wid, "understand")


# ----------------------------------------------- Critical fix: workflow_query_transform_strategy snapshot

#: retrieve.py:18 reads ``agent.settings.workflow_query_transform_strategy``.  Before the
#: fix, workflow creation only captured ``effective_rag_settings`` (base RAG) into
#: ``rag_snapshot`` — ``effective_rag_workflow_settings`` was never snapshotted, so the
#: field stayed at the ``Settings`` class default ``NONE`` on every workflow run.  Users
#: saved ``multi_query``/``decomposition`` in the UI → persisted to SQLite → never read.
#: The fix (option A) captures rag_workflow settings into the snapshot at creation time;
#: ``rag_workflow_overrides()`` maps ``query_transform_strategy`` (payload name) to
#: ``workflow_query_transform_strategy`` (Settings name) so model_copy applies it.


def test_rag_workflow_overrides_maps_payload_to_settings_field_names(tmp_path, monkeypatch):
    """``rag_workflow_overrides()`` must rename ``query_transform_strategy`` → ``workflow_query_transform_strategy``.

    Without this rename, ``model_copy(update=...)`` in ``runtime_settings_from_snapshot``
    would silently ignore the payload key (no matching Settings field) and the retrieve
    node would always see NONE.
    """
    from porto_chatbot import main
    from porto_chatbot.api.deps import rag_workflow_overrides
    from porto_chatbot.config_store import ConfigStore
    from porto_chatbot.models import RagWorkflowSettingsPayload

    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    monkeypatch.setattr(main, "settings", settings)
    ConfigStore(settings).save_rag_workflow_settings(
        RagWorkflowSettingsPayload(query_transform_strategy="multi_query", multi_query_count=5)
    )
    overrides = rag_workflow_overrides()
    assert overrides["workflow_query_transform_strategy"] == "multi_query"
    assert "query_transform_strategy" not in overrides  # renamed away
    assert overrides["multi_query_count"] == 5


def test_build_agent_reads_workflow_query_transform_from_snapshot(tmp_path, monkeypatch):
    """Critical: snapshot ``workflow_query_transform_strategy`` → ``agent.settings``.

    Simulates the fixed creation path: rag_snapshot carries the mapped key, _build_agent
    rebuilds Settings via runtime_settings_from_snapshot, retrieve.py:18 reads it.
    Verifies multi_query (NOT none) reaches the agent that retrieve_knowledge uses.
    """
    from porto_chatbot import main
    from porto_chatbot.models.enums import QueryTransformStrategy

    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    monkeypatch.setattr(main, "settings", settings)
    ex, store = _make(tmp_path)
    rag_snap = {"embedding_provider": "local", "workflow_query_transform_strategy": "multi_query"}
    agent_snap = {"agent_provider": "openai"}
    wid = store.create("s", "p", "prd", 6, rag_snap, agent_snap)
    agent = ex._build_agent(store.get(wid))
    assert agent.settings.workflow_query_transform_strategy == QueryTransformStrategy.MULTI_QUERY


def test_build_agent_defaults_to_none_without_workflow_overrides(tmp_path, monkeypatch):
    """Backward compat: old snapshots (pre-fix, no workflow_query_transform_strategy key)
    still default to NONE — existing workflows/tests are unaffected."""
    from porto_chatbot import main
    from porto_chatbot.models.enums import QueryTransformStrategy

    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    monkeypatch.setattr(main, "settings", settings)
    ex, store = _make(tmp_path)
    # Old-style snapshot: only base RAG keys, no workflow_query_transform_strategy
    rag_snap = {"embedding_provider": "local"}
    agent_snap = {"agent_provider": "openai"}
    wid = store.create("s", "p", "prd", 6, rag_snap, agent_snap)
    agent = ex._build_agent(store.get(wid))
    assert agent.settings.workflow_query_transform_strategy == QueryTransformStrategy.NONE
