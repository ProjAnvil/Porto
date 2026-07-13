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


def _wait_status(
    client: TestClient, wid: str, target: set[str], timeout: float = 15.0
) -> dict:
    """轮询 workflow detail 直到 status 进入 target 集合。降级路径下(无 LLM key)
    workflow 可能很快跑到 completed(理解/识别走 fallback、生成走 template、evaluate
    给分),故 target 通常含 awaiting_input 与 completed 两个目标。
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
        assert detail["current_step"] in {"understand", "evaluate"}
        outs = detail["outputs"]
        assert "understanding" in outs.get("understand", {}).get("output", {}) or detail[
            "current_step"
        ] == "evaluate"


def test_list_and_delete(monkeypatch, sample_settings, sample_prd):
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        _wait_index_done(client)
        r = client.post(
            "/api/porto/workflows", json={"text": sample_prd, "session_id": "s1"}
        )
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
        r = client.post(
            "/api/porto/workflows", json={"text": sample_prd, "session_id": "s1"}
        )
        wid = r.json()["workflow_id"]
        # 立刻连续 advance(第一个可能已过 understand)
        r2 = client.post(f"/api/porto/workflows/{wid}/advance")
        # 至少不 500;若已 completed 返回 200/409 任一可接受
        assert r2.status_code in (200, 409)


def test_put_step_overwrites_and_resets_status(
    monkeypatch, sample_settings, sample_prd
):
    """PUT /steps/{step}:overwrite output(produced_by=user)+ clear_outputs_after
    + current_step=step + status=awaiting_input。step 不在白名单时返回 400。
    """
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        _wait_index_done(client)
        r = client.post(
            "/api/porto/workflows", json={"text": sample_prd, "session_id": "s1"}
        )
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
        bad = client.put(
            f"/api/porto/workflows/{wid}/steps/retrieve", json={"x": 1}
        )
        assert bad.status_code == 400


def test_get_detail_404_for_missing(monkeypatch, sample_settings):
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        assert client.get("/api/porto/workflows/nope").status_code == 404
        assert client.delete("/api/porto/workflows/nope").status_code == 404
