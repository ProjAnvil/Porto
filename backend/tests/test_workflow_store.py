import pytest
from porto_chatbot.workflow_store import WorkflowStore
from porto_chatbot.settings import Settings

STEPS = ["retrieve", "understand", "identify", "generate", "evaluate"]


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
    s.save_output(wid, "understand", {"understanding": "v2"}, "user")  # 覆盖
    outs = s.get_outputs(wid)
    assert outs["understand"]["output"] == {"understanding": "v2"}
    assert outs["understand"]["produced_by"] == "user"


def test_clear_outputs_after(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "p", "prd", 6, {}, {})
    for step in STEPS:
        s.save_output(wid, step, {"x": 1}, "ai")
    s.clear_outputs_after(wid, "understand")  # 删 identify/generate/evaluate
    outs = s.get_outputs(wid)
    assert set(outs.keys()) == {"retrieve", "understand"}


def test_list_filters(tmp_path):
    s = _store(tmp_path)
    w1 = s.create("s1", "p1", "prd", 6, {}, {})
    s.update_status(w1, "completed", current_step="evaluate")
    w2 = s.create("s2", "p2", "prd", 6, {}, {})
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
    # 倒序：offset=0 的应比 offset=2 的新
    assert rows[0]["created_at"] >= rows2[0]["created_at"]
    # date 过滤：今天创建的
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
    s.save_output(wid, "evaluate", {"score": 8}, "ai")

    ok = s.update_spec(wid, "Auth", "新正文")
    assert ok is True
    outs = s.get_outputs(wid)
    assert outs["generate"]["output"]["specs"]["Auth"] == "新正文"
    assert outs["generate"]["output"]["specs"]["Pay"] == "原Pay"  # 其他 spec 不变
    assert outs["generate"]["produced_by"] == "ai"  # 审计字段不变
    assert outs["evaluate"]["output"]["score"] == 8  # 下游不变
    assert "evaluate" in outs


def test_update_spec_missing_returns_false(tmp_path):
    s = _store(tmp_path)
    wid = s.create("sess", "p", "prd", 6, {}, {})
    assert s.update_spec(wid, "Auth", "x") is False  # 无 generate output
    s.save_output(wid, "generate", {"specs": {"Auth": "原"}}, "ai")
    assert s.update_spec(wid, "Nope", "x") is False  # name 不在 specs
    s.save_output(wid, "generate", {"specs": "not a dict"}, "ai")
    assert s.update_spec(wid, "Auth", "x") is False  # specs 非 dict
