from porto_chatbot.agent.graph import STEPS
from porto_chatbot.settings import Settings
from porto_chatbot.workflow_store import WorkflowStore


def _store(tmp_path):
    return WorkflowStore(Settings(data_dir=tmp_path, log_dir=tmp_path / "logs"))


def test_create_and_get(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "proj", "prd", 6, {"r": 1}, {"a": 1})
    row = s.get(wid)
    assert row["workflow_id"] == wid
    assert row["status"] == "created"
    assert row["project_name"] == "proj"
    assert row["current_step"] is None
    assert row["rag_snapshot"] == '{"r": 1}'


def test_save_output_upsert_and_get(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "p", "prd", 6, {}, {})
    s.save_output(wid, "understand", {"understanding": "v1"}, "ai")
    s.save_output(wid, "understand", {"understanding": "v2"}, "user")  # overwrite
    outs = s.get_outputs(wid)
    assert outs["understand"]["output"] == {"understanding": "v2"}
    assert outs["understand"]["produced_by"] == "user"


def test_clear_outputs_after(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "p", "prd", 6, {}, {})
    for step in STEPS:
        s.save_output(wid, step, {"x": 1}, "ai")
    s.clear_outputs_after(wid, "understand")  # removes identify / generate (Task 10: evaluate is gone)
    outs = s.get_outputs(wid)
    assert set(outs.keys()) == {"retrieve", "understand"}


def test_list_filters(tmp_path):
    s = _store(tmp_path)
    w1 = s.create("s1", "p1", "prd", 6, {}, {})
    s.update_status(w1, "completed", current_step="generate")  # Task 10: evaluate is gone
    s.create("s2", "p2", "prd", 6, {}, {})  # a second workflow (its return value is unused here)
    rows, total = s.list_workflows()
    assert total == 2
    assert len(rows) == 2
    rows, total = s.list_workflows(session_id="s1")
    assert total == 1
    assert rows[0]["workflow_id"] == w1
    rows, total = s.list_workflows(status="completed")
    assert total == 1


def test_list_pagination_and_date(tmp_path):
    s = _store(tmp_path)
    for i in range(5):
        s.create(f"s{i}", f"p{i}", "prd", 6, {}, {})
    rows, total = s.list_workflows(limit=2, offset=0)
    assert total == 5
    assert len(rows) == 2
    rows2, _ = s.list_workflows(limit=2, offset=2)
    assert len(rows2) == 2
    # Descending order: the row at offset=0 should be newer than the one at offset=2
    assert rows[0]["created_at"] >= rows2[0]["created_at"]
    # Date filter: created today
    from datetime import UTC, datetime

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    rows, total = s.list_workflows(date=today, limit=20, offset=0)
    assert total == 5
    rows, total = s.list_workflows(date="2099-01-01", limit=20, offset=0)
    assert total == 0


def test_delete(tmp_path):
    s = _store(tmp_path)
    wid = s.create("s", "p", "prd", 6, {}, {})
    s.save_output(wid, "understand", {"u": 1}, "ai")
    s.delete(wid)
    assert s.get(wid) is None
    assert s.get_outputs(wid) == {}


def test_update_spec_updates_only_named_spec(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "p", "prd", 6, {}, {})
    s.save_output(wid, "generate", {"specs": {"Auth": "原", "Pay": "原Pay"}}, "ai")
    # Task 10: the evaluate node is gone, so no evaluate output is created; retrieve serves
    # as the "downstream unchanged" control
    s.save_output(wid, "retrieve", {"sources": []}, "ai")

    ok = s.update_spec(wid, "Auth", "新正文")
    assert ok is True
    outs = s.get_outputs(wid)
    assert outs["generate"]["output"]["specs"]["Auth"] == "新正文"
    assert outs["generate"]["output"]["specs"]["Pay"] == "原Pay"  # other specs are unchanged
    assert outs["generate"]["produced_by"] == "ai"  # the audit field is unchanged
    # The output of other steps must not be changed by PATCH /specs
    assert outs["retrieve"]["output"] == {"sources": []}


def test_update_spec_missing_returns_false(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "p", "prd", 6, {}, {})
    assert s.update_spec(wid, "Auth", "x") is False  # no generate output
    s.save_output(wid, "generate", {"specs": {"Auth": "原"}}, "ai")
    assert s.update_spec(wid, "Nope", "x") is False  # the name is not in specs
    s.save_output(wid, "generate", {"specs": "not a dict"}, "ai")
    assert s.update_spec(wid, "Auth", "x") is False  # specs is not a dict


def test_clear_outputs_after_uses_graph_steps(monkeypatch, tmp_path):
    """F2: the step order of clear_outputs_after comes from agent.graph.STEPS (the single
    source of truth), not from a hard-coded copy.

    After monkeypatching STEPS in the store module, clear_outputs_after should follow it,
    which proves that order references STEPS rather than a constant of its own. Today the
    store hard-codes order and has no STEPS module attribute → this test is RED.
    """
    import porto_chatbot.workflow_store as store_mod

    s = _store(tmp_path)
    wid = s.create("sess", "p", "prd", 6, {}, {})
    for step in ("a", "b", "c"):
        s.save_output(wid, step, {"x": 1}, "ai")
    monkeypatch.setattr(store_mod, "STEPS", ["a", "b", "c"])
    s.clear_outputs_after(wid, "a")  # clears b / c, which come after a
    assert set(s.get_outputs(wid).keys()) == {"a"}


# ---------------------------------------------------------------- Task 7: update_agent_snapshot


def test_update_agent_snapshot_merges(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "proj", "prd", 6, {"r": 1}, {"agent_max_tool_turns": 10, "other": 1})
    ok = s.update_agent_snapshot(wid, {"agent_max_tool_turns": 15})
    assert ok is True
    import json
    snap = json.loads(s.get(wid)["agent_snapshot"])
    assert snap["agent_max_tool_turns"] == 15
    assert snap["other"] == 1  # keys that were not provided are kept


def test_update_agent_snapshot_missing_returns_false(tmp_path):
    s = _store(tmp_path)
    assert s.update_agent_snapshot("nope", {"x": 1}) is False
