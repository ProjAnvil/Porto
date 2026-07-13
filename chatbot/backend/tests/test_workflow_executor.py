"""WorkflowExecutor —— 后台线程推进 workflow + per-workflow 锁防并发 advance。

测试通过 monkeypatch WorkflowRunner.run_to_next_checkpoint 模拟节点执行,
用 executor.wait(id, timeout) 等后台线程结束,实现确定性同步。
"""

import time

from porto_chatbot.settings import Settings
from porto_chatbot.workflow_executor import WorkflowExecutor
from porto_chatbot.workflow_store import WorkflowStore


def _make(tmp_path):
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    return WorkflowExecutor(settings, store), store


def test_start_runs_to_understand_checkpoint(tmp_path, monkeypatch):
    ex, store = _make(tmp_path)
    # mock runner:直接把 state 推到 understand checkpoint
    import porto_chatbot.workflow_executor as we

    def fake_run(agent, state):
        state["current_step"] = "understand"
        state["status"] = "awaiting_input"
        state["sources"] = []
        state["understanding"] = "U"
        return state

    monkeypatch.setattr(we.WorkflowRunner, "run_to_next_checkpoint", staticmethod(fake_run))
    wid = store.create(
        "s", "p", "prd", 6, {"embedding_provider": "local"}, {"agent_provider": "openai"}
    )
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    row = store.get(wid)
    assert row["status"] == "awaiting_input"
    assert row["current_step"] == "understand"
    outs = store.get_outputs(wid)
    assert "understand" in outs


def test_advance_returns_false_when_running(tmp_path, monkeypatch):
    ex, store = _make(tmp_path)
    import porto_chatbot.workflow_executor as we

    def slow_run(agent, state):
        time.sleep(0.3)
        state["current_step"] = "understand"
        state["status"] = "awaiting_input"
        return state

    monkeypatch.setattr(we.WorkflowRunner, "run_to_next_checkpoint", staticmethod(slow_run))
    wid = store.create(
        "s", "p", "prd", 6, {"embedding_provider": "local"}, {"agent_provider": "openai"}
    )
    ex.start_workflow(wid)
    # 不等,直接再 advance —— guard 被后台线程持有,必须返回 False(调用方应 409)
    assert ex.advance(wid) is False
    ex.wait(wid, timeout=5)


def test_failed_records_error(tmp_path, monkeypatch):
    ex, store = _make(tmp_path)
    import porto_chatbot.workflow_executor as we

    def boom(agent, state):
        raise RuntimeError("llm down")

    monkeypatch.setattr(we.WorkflowRunner, "run_to_next_checkpoint", staticmethod(boom))
    wid = store.create(
        "s", "p", "prd", 6, {"embedding_provider": "local"}, {"agent_provider": "openai"}
    )
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    row = store.get(wid)
    assert row["status"] == "failed"
    assert "llm down" in row["error"]


def test_advance_from_checkpoint_runs_next(tmp_path, monkeypatch):
    """advance 从 awaiting_input 状态继续推进到下个 checkpoint。"""
    ex, store = _make(tmp_path)

    def fake_run(agent, state):
        # 从 understand 推进到 identify
        state["current_step"] = "identify"
        state["status"] = "awaiting_input"
        state["subsystems"] = []
        return state

    import porto_chatbot.workflow_executor as we

    monkeypatch.setattr(we.WorkflowRunner, "run_to_next_checkpoint", staticmethod(fake_run))
    wid = store.create(
        "s", "p", "prd", 6, {"embedding_provider": "local"}, {"agent_provider": "openai"}
    )
    # 模拟已经停在 understand checkpoint
    store.update_status(wid, "awaiting_input", current_step="understand")
    store.save_output(wid, "understand", {"understanding": "U"}, "ai")

    assert ex.advance(wid) is True
    ex.wait(wid, timeout=5)
    row = store.get(wid)
    assert row["status"] == "awaiting_input"
    assert row["current_step"] == "identify"
