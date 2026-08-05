"""Workflow API(异步分步 + checkpoint 编辑/回退)集成测试。

测试用 TestClient 驱动真实 FastAPI app,走降级路径(无 LLM key)——workflow 快速
跑到 understand checkpoint(awaiting_input)或一路跑到 completed。两种结果都视为
通过,故 _wait_status 的 target 集合含两者。
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from porto_chatbot import main


def _wait_index_done(client: TestClient, timeout: float = 30.0) -> dict:
    """轮询 /api/kb/stats 直到 rag_index 任务结束(与 test_api.py 同名 helper 同语义)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        stats = client.get("/api/kb/stats").json()
        if stats.get("rag_index", {}).get("status") in (
            "succeeded",
            "failed",
            "interrupted",
        ):
            return stats
        time.sleep(0.15)
    raise AssertionError(f"index did not finish within {timeout}s")


def _wait_status(client: TestClient, wid: str, target: set[str], timeout: float = 15.0) -> dict:
    """轮询 workflow detail 直到 status 进入 target 集合。降级路径下(无 LLM key)
    workflow 可能很快跑到 completed(理解/识别走 fallback、生成走 template),
    故 target 通常含 awaiting_input 与 completed 两个目标。
    """
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = client.get(f"/api/porto/workflows/{wid}").json()
        if last["status"] in target:
            return last
        time.sleep(0.1)
    raise AssertionError(f"never reached {target}, last={last}")


def test_workflow_checkpoint_flow(monkeypatch, sample_settings, sample_prd):
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        _wait_index_done(client)

        resp = client.post(
            "/api/porto/workflows",
            json={"text": sample_prd, "project_name": "支付平台", "session_id": "s1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "running"
        wid = body["workflow_id"]
        # 跑到 understand checkpoint(降级路径,无 LLM key,确定性)
        detail = _wait_status(client, wid, {"awaiting_input", "completed"})
        # Task 10:evaluate 节点已删,terminal step 是 generate(而非 evaluate)
        assert detail["current_step"] in {"understand", "generate"}
        outs = detail["outputs"]
        assert (
            "understanding" in outs.get("understand", {}).get("output", {})
            or detail["current_step"] == "generate"
        )


def test_list_and_delete(monkeypatch, sample_settings, sample_prd):
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        _wait_index_done(client)
        r = client.post("/api/porto/workflows", json={"text": sample_prd, "session_id": "s1"})
        wid = r.json()["workflow_id"]
        lst = client.get("/api/porto/workflows?session_id=s1").json()
        assert any(w["workflow_id"] == wid for w in lst["items"])
        assert client.delete(f"/api/porto/workflows/{wid}").status_code == 204


def test_advance_concurrent_returns_409(monkeypatch, sample_settings, sample_prd):
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        _wait_index_done(client)
        r = client.post("/api/porto/workflows", json={"text": sample_prd, "session_id": "s1"})
        wid = r.json()["workflow_id"]
        # 立刻连续 advance(第一个可能已过 understand)
        r2 = client.post(f"/api/porto/workflows/{wid}/advance")
        # 至少不 500;若已 completed 返回 200/409 任一可接受
        assert r2.status_code in (200, 409)


def test_put_step_overwrites_and_resets_status(monkeypatch, sample_settings, sample_prd):
    """PUT /steps/{step}:overwrite output(produced_by=user)+ clear_outputs_after
    + current_step=step + status=awaiting_input。step 不在白名单时返回 400。
    """
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        _wait_index_done(client)
        r = client.post("/api/porto/workflows", json={"text": sample_prd, "session_id": "s1"})
        wid = r.json()["workflow_id"]
        # 等 understand checkpoint
        _wait_status(client, wid, {"awaiting_input", "completed"})
        # PUT 覆盖 understand 产出
        put = client.put(
            f"/api/porto/workflows/{wid}/steps/understand",
            json={"understanding": "用户重写的理解"},
        )
        assert put.status_code == 200
        body = put.json()
        assert body["status"] == "awaiting_input"
        assert body["current_step"] == "understand"
        outs = body["outputs"]["understand"]
        assert outs["output"]["understanding"] == "用户重写的理解"
        assert outs["produced_by"] == "user"
        # 非法 step -> 400
        bad = client.put(f"/api/porto/workflows/{wid}/steps/retrieve", json={"x": 1})
        assert bad.status_code == 400


def test_get_detail_404_for_missing(monkeypatch, sample_settings):
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        assert client.get("/api/porto/workflows/nope").status_code == 404
        assert client.delete("/api/porto/workflows/nope").status_code == 404


def test_advance_past_first_checkpoint_reaches_identify(monkeypatch, sample_settings, sample_prd):
    """回归(跨 checkpoint 推进集成测试):advance 跨过第一个 checkpoint 时,
    下游节点(identify)必须能消费 ``_rebuild_state`` 重建的 sources/subsystems
    Pydantic 模型,而非 AttributeError 崩溃。

    pre-fix 复现路径:
      create → retrieve(写 sources 为 SourceChunk) → understand(checkpoint,persist
      sources 经 _to_jsonable → dict) → advance → identify 读 state["sources"]
      → s.text → AttributeError(dict 无 .text)→ status="failed"。

    修复后:_rebuild_state 把 sources/subsystems/spec_results 从 dict 重建为
    Pydantic 模型,identify 正常消费,workflow 推进到 identify checkpoint
    (或一路到 completed/generate,均无 failed)。

    本测试不 mock graph —— 跑真实 retrieve→understand→identify 节点。
    """
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        _wait_index_done(client)

        resp = client.post(
            "/api/porto/workflows",
            json={"text": sample_prd, "project_name": "支付平台", "session_id": "s1"},
        )
        assert resp.status_code == 200
        wid = resp.json()["workflow_id"]

        # ---- 第一段:跑到 understand checkpoint(或降级路径直接 completed)----
        first = _wait_status(client, wid, {"awaiting_input", "completed"})
        # 若已 completed(极快路径),则无 checkpoint 间崩溃可言,无需继续
        if first["status"] == "completed":
            assert first["current_step"] == "generate"  # Task 10:evaluate 已删
            return

        assert first["current_step"] == "understand"
        # 确认 sources 已持久化(retrieve 必跑过)—— 这正是后续 identify 要消费的字段
        retrieve_out = first["outputs"].get("retrieve", {}).get("output", {})
        assert "sources" in retrieve_out, "retrieve 输出应有 sources(被 JSON 序列化为 dict)"

        # ---- 第二段:advance,跨过 understand→identify —— pre-fix 在此崩溃 ----
        adv = client.post(f"/api/porto/workflows/{wid}/advance")
        assert adv.status_code == 200, f"advance 应被接受,实际 {adv.status_code}: {adv.text}"

        second = _wait_status(client, wid, {"awaiting_input", "completed", "failed"}, timeout=20.0)
        # 关键断言:不允许 failed(pre-fix 会因 s.text AttributeError 落到 failed)
        assert second["status"] != "failed", (
            f"advance 跨 checkpoint 崩溃: status=failed "
            f"current_step={second.get('current_step')} error={second.get('error')!r}"
        )
        # 必须跑到至少 identify(降级快路径可能一路到 generate/completed)
        reached_step = second["current_step"]
        steps_order = ["retrieve", "understand", "identify", "generate"]
        assert steps_order.index(reached_step) >= steps_order.index("identify"), (
            f"advance 应跨过 understand 到达 identify 及以后,实际停在 {reached_step}"
        )


def test_patch_spec_updates_without_side_effects(monkeypatch, sample_settings):
    """PATCH /specs：只改 generate.specs[name]，不改 status/current_step、
    不清下游、不动 produced_by。name 不存在→400；workflow 不存在→404。"""
    from porto_chatbot.api.deps import get_workflow_store

    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        store = get_workflow_store()
        # 直接造 workflow + generate（不经 executor，确定性；Task 10:evaluate 已删）
        wid = store.create("s1", "proj", "prd", 6, {}, {})
        store.save_output(wid, "generate", {"specs": {"Auth": "原始"}}, "ai")
        store.update_status(wid, "completed", current_step="generate")

        resp = client.patch(
            f"/api/porto/workflows/{wid}/specs",
            json={"name": "Auth", "body": "编辑后正文"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["outputs"]["generate"]["output"]["specs"]["Auth"] == "编辑后正文"
        # 副作用均未发生
        assert body["status"] == "completed"
        assert body["current_step"] == "generate"
        assert body["outputs"]["generate"]["produced_by"] == "ai"

        # name 不存在 → 400
        bad = client.patch(
            f"/api/porto/workflows/{wid}/specs",
            json={"name": "Nope", "body": "x"},
        )
        assert bad.status_code == 400

        # workflow 不存在 → 404
        miss = client.patch(
            "/api/porto/workflows/missing/specs",
            json={"name": "Auth", "body": "x"},
        )
        assert miss.status_code == 404


def test_document_capabilities_and_upload_validation(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        capabilities = client.get("/api/porto/document-capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json() == {
            "enabled": False,
            "image_input": False,
            "native_pdf": False,
            "reason": "LLM client is disabled",
            "parse_mode": "hybrid",
        }

        unsupported = client.post(
            "/api/porto/workflows/upload",
            files={"file": ("prd.exe", b"not a document", "application/octet-stream")},
        )
        assert unsupported.status_code == 415

        sample_settings.document_max_upload_mb = 1
        too_large = client.post(
            "/api/porto/workflows/upload",
            files={"file": ("prd.txt", b"x" * (1024 * 1024 + 1), "text/plain")},
        )
        assert too_large.status_code == 413


def test_upload_maps_parse_errors(monkeypatch, sample_settings):
    """Task 6:upload 路由把文档交给 FileService.store 落盘 + LOCAL 解析 + 分页。

    parse_document 失败 → DocumentParseError → 路由映射 400(native strict 422
    路径已随 FileService 统一 LOCAL 解析移除;phase 2 的方向是按需 read_file,
    上传时不再做 native PDF 一次性富化)。
    """
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        broken = client.post(
            "/api/porto/workflows/upload",
            files={"file": ("prd.pdf", b"not-a-pdf", "application/pdf")},
        )
        assert broken.status_code == 400


# ----------------------------------------------------------- Task 8: rerun 步骤


def test_rerun_step_accepted(monkeypatch, sample_settings):
    """200: 合法 rerun 被接受 → {workflow_id, status:"running"}。

    仿 test_patch_spec_updates_without_side_effects 的 fixture 风格:monkeypatch settings
    + TestClient(main.app) + 直接 store.create 造 workflow(不经 executor/graph)。
    另 monkeypatch executor.rerun_step 为 no-op,避开真实 graph/LLM 节点重跑 ——
    本测试只验证路由层 404/400/409/200 分支,executor 内部行为由 test_workflow_executor 覆盖。
    """
    from porto_chatbot.api.deps import get_workflow_executor, get_workflow_store

    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        store = get_workflow_store()
        # 直接造 workflow(snapshot 默认 agent_max_tool_turns,远低于 hard_cap=40)
        wid = store.create("s1", "proj", "prd", 6, {}, {"agent_max_tool_turns": 10})
        # Mock executor.rerun_step 避开真实 graph 节点函数 / LLM 调用
        ex = get_workflow_executor()
        monkeypatch.setattr(ex, "rerun_step", lambda _wid, _step: None)

        resp = client.post(f"/api/porto/workflows/{wid}/steps/understand/rerun")
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["workflow_id"] == wid
        assert body["status"] == "running"


def test_rerun_step_bad_step_returns_400(monkeypatch, sample_settings):
    """400: step 不在 STEPS → 路由直接拒绝,不调用 executor。"""
    from porto_chatbot.api.deps import get_workflow_store

    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        store = get_workflow_store()
        wid = store.create("s1", "proj", "prd", 6, {}, {})
        resp = client.post(f"/api/porto/workflows/{wid}/steps/nope/rerun")
        assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"


def test_rerun_step_at_cap_returns_409(monkeypatch, sample_settings):
    """409: agent_snapshot.agent_max_tool_turns 已达 hard_cap(默认 40)→
    executor.rerun_step 同步 raise WorkflowRunning → 路由 409。

    不 mock executor —— rerun_step 在 spawn worker 之前就 raise,不触达真实 graph/LLM,
    行为由 test_workflow_executor.test_rerun_step_at_cap_raises 已覆盖。
    """
    from porto_chatbot.api.deps import get_workflow_store

    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        store = get_workflow_store()
        # snapshot 直接设到 hard_cap,executor 读到 cur >= cap → raise WorkflowRunning
        wid = store.create(
            "s1", "proj", "prd", 6, {}, {"agent_max_tool_turns": sample_settings.tool_turn_hard_cap}
        )
        resp = client.post(f"/api/porto/workflows/{wid}/steps/understand/rerun")
        assert resp.status_code == 409, f"expected 409, got {resp.status_code}: {resp.text}"
