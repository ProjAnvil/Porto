"""WorkflowExecutor —— 后台线程推进 workflow + per-workflow 锁防并发 advance。

测试通过 monkeypatch WorkflowRunner.run_to_next_checkpoint 模拟节点执行,
用 executor.wait(id, timeout) 等后台线程结束,实现确定性同步。
"""

import threading
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


def test_two_rapid_advances_first_wins(tmp_path, monkeypatch):
    """回归:两次 rapid advance() 同一 workflow,第一次必须 True,第二次必须 False。

    修复前 _run_async 模式:acquire(non-blocking) → release → blocking acquire
    在 release 与 re-acquire 之间出现 TOCTOU 窗口,两次并发 advance 都能拿到 True
    并各起 worker,跳过 checkpoint。修复后 guard 跨越 Thread.start() 不被 release,
    第二次 advance 的非阻塞 acquire 必然失败。
    """
    ex, store = _make(tmp_path)
    import porto_chatbot.workflow_executor as we

    started = threading.Event()

    def slow_run(agent, state):
        # 模拟长任务:先发信号告知 worker 已进入,再 sleep;此时第二次 advance 必然失败
        started.set()
        time.sleep(0.5)
        state["current_step"] = "understand"
        state["status"] = "awaiting_input"
        state["sources"] = []
        state["understanding"] = "U"
        return state

    monkeypatch.setattr(we.WorkflowRunner, "run_to_next_checkpoint", staticmethod(slow_run))
    wid = store.create(
        "s", "p", "prd", 6, {"embedding_provider": "local"}, {"agent_provider": "openai"}
    )
    ex.start_workflow(wid)
    # 等 worker 真正进入 slow_run(此时 guard 被持有)
    assert started.wait(timeout=2.0), "worker 未启动"
    # 此时两次 advance 都应被拒绝(workflow 在 running)
    r1 = ex.advance(wid)
    r2 = ex.advance(wid)
    assert r1 is False, f"第一次 advance 期望 False,实际 {r1}"
    assert r2 is False, f"第二次 advance 期望 False,实际 {r2}"
    ex.wait(wid, timeout=5)


def test_persist_state_preserves_user_produced_by(tmp_path, monkeypatch):
    """回归:_persist_state 只落本次跑过的步;更早的步(已 user 编辑)produced_by 保持 user。

    场景:understand 步已用户编辑(produced_by="user"),本次 advance 从 understand
    推进到 identify。修复前 _persist_state 会重写 understand 的 produced_by="ai",
    污染审计轨迹。修复后只写 identify。
    """
    ex, store = _make(tmp_path)

    def fake_run(agent, state):
        state["current_step"] = "identify"
        state["status"] = "awaiting_input"
        state["subsystems"] = []
        return state

    import porto_chatbot.workflow_executor as we

    monkeypatch.setattr(we.WorkflowRunner, "run_to_next_checkpoint", staticmethod(fake_run))
    wid = store.create(
        "s", "p", "prd", 6, {"embedding_provider": "local"}, {"agent_provider": "openai"}
    )
    # 模拟 understand 已停在 checkpoint,且用户编辑过(produced_by="user")
    store.update_status(wid, "awaiting_input", current_step="understand")
    store.save_output(wid, "understand", {"understanding": "user-edited"}, "user")

    assert ex.advance(wid) is True
    ex.wait(wid, timeout=5)

    outs = store.get_outputs(wid)
    # understand 的 produced_by 必须仍是 user(未被 _persist_state 覆盖)
    assert outs["understand"]["produced_by"] == "user", (
        f"understand produced_by 被覆盖: {outs['understand']['produced_by']!r}"
    )
    # identify 是本次跑的,应为 ai
    assert outs["identify"]["produced_by"] == "ai"
