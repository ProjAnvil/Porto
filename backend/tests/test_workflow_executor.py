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

    关键点:第一次 True 必须由 **advance()** 返回(不是 start_workflow 预占 guard)。
    否则 release-reacquire race 无法被检出 —— start_workflow 持有 guard 时两次 advance
    都返回 False,即便旧实现的 _run_async release→blocking acquire race 也不会暴露。

    场景:
    1. start_workflow + FAST runner 把 workflow 推到 understand checkpoint 并结束
       (guard 已释放,无 worker 在跑)。
    2. 换 SLOW runner:进入时 set(running),阻塞于 release.wait() —— worker 长时间持有 guard。
    3. 第一次 advance():guard 空闲 → 拿到 → 起 worker → 返回 **True**(关键断言)。
    4. 等 running 被 set → 确认 worker 已进入 runner、guard 被持有。
    5. 第二次 advance():guard 被持 → 非阻塞 acquire 失败 → 返回 **False**(关键断言)。
    6. set(release) 让 worker 完成;最终 current_step == "identify" 证明第一次 advance
       确实驱动了一步。

    若有人回滚到 release-reacquire 模式(advance 内部先 release 再 blocking acquire),
    第二次 advance 可能抢到 True → 测试失败。
    """
    ex, store = _make(tmp_path)
    import porto_chatbot.workflow_executor as we

    # ---- Step 1: FAST runner,只为 start_workflow 把 workflow 推到 understand checkpoint
    def fast_run(agent, state):
        state["current_step"] = "understand"
        state["status"] = "awaiting_input"
        state["sources"] = []
        state["understanding"] = "U"
        return state

    monkeypatch.setattr(we.WorkflowRunner, "run_to_next_checkpoint", staticmethod(fast_run))
    wid = store.create(
        "s", "p", "prd", 6, {"embedding_provider": "local"}, {"agent_provider": "openai"}
    )
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    # 现在 workflow 停在 understand checkpoint,guard 空闲,无 worker 在跑
    assert store.get(wid)["current_step"] == "understand"

    # ---- Step 2: 换 SLOW runner,worker 进入后阻塞,持续持有 guard
    running = threading.Event()
    release = threading.Event()

    def slow_run(agent, state):
        running.set()
        release.wait(timeout=5.0)
        # 从 understand 推进到 identify
        state["current_step"] = "identify"
        state["status"] = "awaiting_input"
        state["subsystems"] = []
        return state

    monkeypatch.setattr(we.WorkflowRunner, "run_to_next_checkpoint", staticmethod(slow_run))

    # ---- Step 3: 第一次 advance 必须返回 True(advance 自己拿到 guard 并起 worker)
    r1 = ex.advance(wid)
    assert r1 is True, f"第一次 advance 期望 True,实际 {r1}(race 检测已失效)"

    # ---- Step 4: 等 worker 进入 slow_run(guard 已被 worker 持有)
    assert running.wait(timeout=2.0), "worker 未进入 slow_run"

    # ---- Step 5: 第二次 advance 必须返回 False(guard 被 worker 持有)
    r2 = ex.advance(wid)
    assert r2 is False, f"第二次 advance 期望 False,实际 {r2}(guard 未跨 Thread.start 持有)"

    # ---- Step 6: 唤醒 worker 完成,验证第一次 advance 驱动了一步
    release.set()
    ex.wait(wid, timeout=5)
    row = store.get(wid)
    assert row["status"] == "awaiting_input", f"期望 awaiting_input,实际 {row['status']}"
    assert row["current_step"] == "identify", f"期望 identify,实际 {row['current_step']}"


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
