"""WorkflowExecutor —— 后台线程用 langgraph graph 推进 + per-workflow 锁防并发 advance。

测试用 trivial graph(stand-in 节点 + 临时 SqliteSaver)注入 executor,验证:
start/advance/failed/并发 guard/投影保 produced_by。不跑真节点逻辑。
"""
from __future__ import annotations

import sqlite3
import threading

from langgraph.graph import END, START, StateGraph

from porto_chatbot.agent.graph import STEPS
from porto_chatbot.settings import Settings
from porto_chatbot.workflow_executor import WorkflowExecutor
from porto_chatbot.workflow_store import WorkflowStore


def _saver(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "ex.sqlite3"), check_same_thread=False)
    from langgraph.checkpoint.sqlite import SqliteSaver
    sv = SqliteSaver(conn)
    sv.setup()
    return sv


#: stand-in 节点每个 step 写的产出(注意 specs/spec_results 必须是 dict ——
#: PortoAgentState 上它们带 dict-merge reducer,传字符串会让 reducer 崩)。
_OUT_VALS = {
    "retrieve": {"sources": "retrieve-val"},
    "understand": {"understanding": "understand-val"},
    "identify": {"subsystems": "identify-val"},
    "generate": {"specs": {"default": "generate-val"}, "spec_results": {"default": "gen"}},
    "evaluate": {"evaluation": "evaluate-val"},
}


def _trivial_graph(tmp_path, *, fail_on=None, slow_step=None, slow_event=None):
    """与真图同拓扑 + interrupt_after 的 stand-in 图;每节点写产出键 + current_step。

    fail_on: 命中该 step 时抛异常(测 failed 路径)。
    slow_step + slow_event: 仅当运行到 slow_step 时 set(enter) 并阻塞于 release.wait
        (测 guard:让 worker 进入该节点后持续持 guard;其余节点不阻塞)。
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
    return g.compile(checkpointer=_saver(tmp_path),
                     interrupt_after=["understand", "identify", "generate"])


def _make(tmp_path, **kw):
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    ex = WorkflowExecutor(settings, store, _trivial_graph(tmp_path, **kw))
    return ex, store


def _create(store):
    return store.create("s", "p", "prd", 6, {"embedding_provider": "local"},
                        {"agent_provider": "openai"})


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
    for _ in range(3):  # understand→identify→generate→evaluate(END)
        assert ex.advance(wid) is True
        ex.wait(wid, timeout=5)
    assert store.get(wid)["status"] == "completed"
    assert store.get(wid)["current_step"] == "evaluate"


def test_advance_returns_false_when_running(tmp_path):
    """advance 时 worker 进入 slow 节点(identify)持续持 guard:再 advance 必 False。"""
    enter, release = threading.Event(), threading.Event()
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    ex = WorkflowExecutor(
        settings, store,
        _trivial_graph(tmp_path, slow_step="identify", slow_event=(enter, release)),
    )
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)   # 停 understand;identify 未触发,不阻塞
    assert ex.advance(wid) is True                     # worker 进 identify → 阻塞、持 guard
    assert enter.wait(timeout=2.0)
    assert ex.advance(wid) is False                    # guard 被持 → 409 语义
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
    """advance 后,更早的(用户编辑过)步 produced_by 保持 user;新步为 ai。

    模拟 understand 已是 produced_by=user(store 直接造),advance 跑 identify。
    投影:understand 内容来自 graph state(stand-in 的 "understand-val",与 store 的
    "user-edited" 不同 → 视为变化 → 重写,但 produced_by 取既有 "user" 保留);identify 新步为 ai。
    """
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)          # 停 understand
    store.save_output(wid, "understand", {"understanding": "user-edited"}, "user")

    assert ex.advance(wid) is True
    ex.wait(wid, timeout=5)                                   # → identify

    outs = store.get_outputs(wid)
    assert outs["understand"]["produced_by"] == "user"        # 未被覆盖为 ai
    assert outs["identify"]["produced_by"] == "ai"


def test_two_rapid_advances_first_wins(tmp_path):
    """回归:两次 rapid advance 同一 workflow,第一次 True(advance 自己拿 guard 起worker),
    第二次 False(guard 被 worker 持有)。单图、slow_step=identify,不换图不换 saver。"""
    enter, release = threading.Event(), threading.Event()
    settings = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs", embedding_provider="local")
    store = WorkflowStore(settings)
    ex = WorkflowExecutor(
        settings, store,
        _trivial_graph(tmp_path, slow_step="identify", slow_event=(enter, release)),
    )
    wid = _create(store)
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)          # 停 understand,guard 空闲

    r1 = ex.advance(wid)
    assert r1 is True                                         # advance 自己拿 guard 起 worker
    assert enter.wait(timeout=2.0)                            # worker 已进 identify、持 guard
    r2 = ex.advance(wid)
    assert r2 is False                                        # guard 被持
    release.set()
    ex.wait(wid, timeout=5)
    assert store.get(wid)["current_step"] == "identify"


# ---------------------------------------------------------------- Task 5: PUT / PATCH / recover


def test_update_step_rewinds_and_marks_user(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)                  # understand
    ex.wait(wid, timeout=5)
    ex.advance(wid)                         # identify
    ex.wait(wid, timeout=5)
    assert "identify" in store.get_outputs(wid)

    ex.update_step(wid, "understand", {"understanding": "edited"})
    row = store.get(wid)
    assert row["current_step"] == "understand"
    assert row["status"] == "awaiting_input"
    outs = store.get_outputs(wid)
    assert outs["understand"]["produced_by"] == "user"
    assert outs["understand"]["output"]["understanding"] == "edited"
    assert "identify" not in outs                           # 下游被清


def test_update_step_then_advance_preserves_user_edit(tmp_path):
    """Important #1: PUT 编辑后 advance,用户编辑的内容必须存活(而非被节点原始产出覆盖)。

    机制:update_step 的 graph.update_state(as_node=understand) 把 edited 内容写进
    graph checkpoint;advance 时 understand 节点不重跑(已位于其下游),_project_state
    读到 graph-state 的 understanding == store 的 edited → skip-if-equal → 保留。
    断言:advance 到 identify 后,store 里 understand.output.understanding 仍是用户编辑值。
    """
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)                  # 停 understand(原始 "understand-val")
    ex.wait(wid, timeout=5)
    ex.advance(wid)                         # → identify(原始 "identify-val")
    ex.wait(wid, timeout=5)

    # 用户编辑 understand(覆盖原始 "understand-val" → "user-edited")
    ex.update_step(wid, "understand", {"understanding": "user-edited"})
    outs = store.get_outputs(wid)
    assert outs["understand"]["output"]["understanding"] == "user-edited"
    assert outs["understand"]["produced_by"] == "user"

    # advance:identify 节点会重跑(下游被 update_step 清掉);understand 不重跑
    assert ex.advance(wid) is True
    ex.wait(wid, timeout=5)

    # 关键断言:understand 内容仍是用户的 edited 值(未被节点原始 "understand-val" 覆盖)
    outs = store.get_outputs(wid)
    assert outs["understand"]["output"]["understanding"] == "user-edited", (
        "user-edited content lost after advance!"
    )
    assert outs["understand"]["produced_by"] == "user"     # 审计字段也保留
    assert "identify" in outs                              # identify 重跑后回来


def test_update_spec_updates_store_and_graph(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    # 跑到 generate checkpoint(trivial 图 generate 写 specs={"default": ...})
    ex.start_workflow(wid)
    ex.wait(wid, timeout=5)
    for _ in range(2):  # → identify → generate
        ex.advance(wid)
        ex.wait(wid, timeout=5)
    assert store.get(wid)["current_step"] == "generate"

    ok = ex.update_spec(wid, "default", "new body")          # 改 specs 里的 "default" key
    assert ok is True
    outs = store.get_outputs(wid)
    assert outs["generate"]["produced_by"] == "ai"           # 审计不动
    assert outs["generate"]["output"]["specs"]["default"] == "new body"
    # graph state 已 dict-merge(specs 是 dict-merge reducer)
    cfg = {"configurable": {"thread_id": wid}}
    assert ex.graph.get_state(cfg).values["specs"]["default"] == "new body"


def test_update_spec_missing_returns_false(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    assert ex.update_spec(wid, "nope", "x") is False        # 无 generate output


def test_recover_at_interrupt_marks_awaiting(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    ex.start_workflow(wid)                 # understand(checkpoint 在)
    ex.wait(wid, timeout=5)
    store.update_status(wid, "running")                     # 模拟崩溃时 status=running
    n = ex.recover_on_startup()
    assert n == 1
    assert store.get(wid)["status"] == "awaiting_input"     # checkpoint 在 interrupt → 可续


def test_recover_no_checkpoint_marks_interrupted(tmp_path):
    ex, store = _make(tmp_path)
    wid = _create(store)
    store.update_status(wid, "running", current_step="understand")  # 从未跑过 graph
    n = ex.recover_on_startup()
    assert n == 1
    row = store.get(wid)
    assert row["status"] == "interrupted"
    assert row["current_step"] == "understand"              # 无 checkpoint → 保既有
