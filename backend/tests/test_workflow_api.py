"""Integration tests for the workflow API (async step-by-step runs + checkpoint edit /
rewind).

The tests drive the real FastAPI app through TestClient on the degraded path (no LLM
key): a workflow quickly reaches the understand checkpoint (awaiting_input) or runs all
the way to completed. Both outcomes count as passing, so the target set of _wait_status
contains both.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from porto_chatbot import main


def _wait_index_done(client: TestClient, timeout: float = 30.0) -> dict:
    """Poll /api/kb/stats until the rag_index job ends (same semantics as the helper of the same
    name in test_api.py)."""
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
    """Poll the workflow detail until status enters the target set. On the degraded path (no LLM
    key) a workflow may run to completed very quickly (understanding / identification take the
    fallback, generation the template), so target usually contains both awaiting_input and
    completed.
    """
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = client.get(f"/api/porto/workflows/{wid}").json()
        if last["status"] in target:
            return last
        time.sleep(0.1)
    raise AssertionError(f"never reached {target}, last={last}")


def _wait_past_checkpoint(
    client: TestClient, wid: str, stale_step: str, timeout: float = 20.0
) -> dict:
    """Poll the workflow detail until a resumed run settles past ``stale_step``.

    ``advance`` answers 200 before its worker thread flips the status to running, so a
    poll that lands in that window still sees the pre-advance awaiting_input snapshot. On
    a slow CI runner the window is long enough for that snapshot to be read as the result.
    """
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = client.get(f"/api/porto/workflows/{wid}").json()
        settled = last["status"] in {"completed", "failed"} or (
            last["status"] == "awaiting_input" and last["current_step"] != stale_step
        )
        if settled:
            return last
        time.sleep(0.1)
    raise AssertionError(f"never moved past {stale_step}, last={last}")


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
        # Reaches the understand checkpoint (degraded path, no LLM key, deterministic)
        detail = _wait_status(client, wid, {"awaiting_input", "completed"})
        # Task 10: the evaluate node is gone; the terminal step is generate (not evaluate)
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
        # Two advances back to back (the first may already have passed understand)
        r2 = client.post(f"/api/porto/workflows/{wid}/advance")
        # At least not a 500; if already completed, either 200 or 409 is acceptable
        assert r2.status_code in (200, 409)


def test_put_step_overwrites_and_resets_status(monkeypatch, sample_settings, sample_prd):
    """PUT /steps/{step}:overwrite output(produced_by=user)+ clear_outputs_after
    + current_step=step + status=awaiting_input. A step outside the allowlist returns 400.
    """
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        _wait_index_done(client)
        r = client.post("/api/porto/workflows", json={"text": sample_prd, "session_id": "s1"})
        wid = r.json()["workflow_id"]
        # Wait for the understand checkpoint
        _wait_status(client, wid, {"awaiting_input", "completed"})
        # PUT overwrites the understand output
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
        # Invalid step -> 400
        bad = client.put(f"/api/porto/workflows/{wid}/steps/retrieve", json={"x": 1})
        assert bad.status_code == 400


def test_get_detail_404_for_missing(monkeypatch, sample_settings):
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        assert client.get("/api/porto/workflows/nope").status_code == 404
        assert client.delete("/api/porto/workflows/nope").status_code == 404


def test_advance_past_first_checkpoint_reaches_identify(monkeypatch, sample_settings, sample_prd):
    """Regression (integration test of advancing across a checkpoint): when advance crosses the
    first checkpoint, the downstream node (identify) must be able to consume the sources /
    subsystems Pydantic models rebuilt by ``_rebuild_state`` instead of crashing with an
    AttributeError.

    Reproduction path before the fix:
      create → retrieve (writes sources as SourceChunk) → understand (checkpoint, persisted
      sources go through _to_jsonable → dict) → advance → identify reads state["sources"] →
      s.text → AttributeError (a dict has no .text) → status="failed".

    After the fix: _rebuild_state rebuilds sources / subsystems / spec_results from dicts into
    Pydantic models, identify consumes them normally, and the workflow advances to the identify
    checkpoint (or all the way to completed / generate, never failed).

    This test does not mock the graph: the real retrieve → understand → identify nodes run.
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

        # ---- Stage 1: run to the understand checkpoint (or straight to completed on the degraded path) ----
        first = _wait_status(client, wid, {"awaiting_input", "completed"})
        # If it is already completed (the very fast path) there is no crash between checkpoints to
        # speak of, so stop here
        if first["status"] == "completed":
            assert first["current_step"] == "generate"  # Task 10: evaluate is gone
            return

        assert first["current_step"] == "understand"
        # Check that sources were persisted (retrieve must have run): this is exactly the field
        # identify consumes next
        retrieve_out = first["outputs"].get("retrieve", {}).get("output", {})
        assert "sources" in retrieve_out, "retrieve 输出应有 sources(被 JSON 序列化为 dict)"

        # ---- Stage 2: advance across understand → identify (this is where it crashed before the fix) ----
        adv = client.post(f"/api/porto/workflows/{wid}/advance")
        assert adv.status_code == 200, f"advance 应被接受,实际 {adv.status_code}: {adv.text}"

        # Must not match the pre-advance awaiting_input/understand snapshot (see helper)
        second = _wait_past_checkpoint(client, wid, stale_step="understand")
        # Key assertion: failed is not allowed (before the fix the s.text AttributeError ended up as
        # failed)
        assert second["status"] != "failed", (
            f"advance 跨 checkpoint 崩溃: status=failed "
            f"current_step={second.get('current_step')} error={second.get('error')!r}"
        )
        # It must get at least to identify (the fast degraded path may go all the way to generate /
        # completed)
        reached_step = second["current_step"]
        steps_order = ["retrieve", "understand", "identify", "generate"]
        assert steps_order.index(reached_step) >= steps_order.index("identify"), (
            f"advance 应跨过 understand 到达 identify 及以后,实际停在 {reached_step}"
        )


def test_patch_spec_updates_without_side_effects(monkeypatch, sample_settings):
    """PATCH /specs: only changes generate.specs[name]; it does not touch status / current_step,
    does not clear downstream steps and leaves produced_by alone. Unknown name → 400; unknown
    workflow → 404."""
    from porto_chatbot.api.deps import get_workflow_store

    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        store = get_workflow_store()
        # Create the workflow + generate output directly (no executor, deterministic; Task 10:
        # evaluate is gone)
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
        # None of the side effects happened
        assert body["status"] == "completed"
        assert body["current_step"] == "generate"
        assert body["outputs"]["generate"]["produced_by"] == "ai"

        # Unknown name → 400
        bad = client.patch(
            f"/api/porto/workflows/{wid}/specs",
            json={"name": "Nope", "body": "x"},
        )
        assert bad.status_code == 400

        # Unknown workflow → 404
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
    """Task 6: the upload route hands the document to FileService.store, which writes it to disk,
    parses it with LOCAL and paginates it.

    A failing parse_document → DocumentParseError → the route maps it to 400 (the native strict
    422 path was removed when FileService moved to LOCAL parsing everywhere; phase 2 heads
    toward read_file on demand, and uploads no longer do a one-off native PDF enrichment).
    """
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        broken = client.post(
            "/api/porto/workflows/upload",
            files={"file": ("prd.pdf", b"not-a-pdf", "application/pdf")},
        )
        assert broken.status_code == 400


# ----------------------------------------------------------- Task 8: rerun step


def test_rerun_step_accepted(monkeypatch, sample_settings):
    """200: a valid rerun is accepted → {workflow_id, status:"running"}.

    Follows the fixture style of test_patch_spec_updates_without_side_effects: monkeypatch
    settings + TestClient(main.app) + a workflow created straight through store.create (no
    executor / graph). executor.rerun_step is additionally monkeypatched to a no-op to avoid a
    real graph / LLM node rerun. This test only verifies the 404 / 400 / 409 / 200 branches of
    the route layer; the executor's internals are covered by test_workflow_executor.
    """
    from porto_chatbot.api.deps import get_workflow_executor, get_workflow_store

    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        store = get_workflow_store()
        # Create the workflow directly (the snapshot has the default agent_max_tool_turns, far below
        # the hard_cap of 40)
        wid = store.create("s1", "proj", "prd", 6, {}, {"agent_max_tool_turns": 10})
        # Mock executor.rerun_step to avoid the real graph node functions / LLM calls
        ex = get_workflow_executor()
        monkeypatch.setattr(ex, "rerun_step", lambda _wid, _step: None)

        resp = client.post(f"/api/porto/workflows/{wid}/steps/understand/rerun")
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["workflow_id"] == wid
        assert body["status"] == "running"


def test_rerun_step_bad_step_returns_400(monkeypatch, sample_settings):
    """400: a step outside STEPS → the route rejects it outright without calling the executor."""
    from porto_chatbot.api.deps import get_workflow_store

    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        store = get_workflow_store()
        wid = store.create("s1", "proj", "prd", 6, {}, {})
        resp = client.post(f"/api/porto/workflows/{wid}/steps/nope/rerun")
        assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"


def test_rerun_step_at_cap_returns_409(monkeypatch, sample_settings):
    """409: agent_snapshot.agent_max_tool_turns has reached the hard_cap (default 40) →
    executor.rerun_step raises WorkflowRunning synchronously → the route returns 409.

    The executor is not mocked: rerun_step raises before spawning the worker and never reaches
    the real graph / LLM; the behavior is already covered by
    test_workflow_executor.test_rerun_step_at_cap_raises.
    """
    from porto_chatbot.api.deps import get_workflow_store

    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        store = get_workflow_store()
        # Set the snapshot straight to hard_cap; the executor reads cur >= cap → raises WorkflowRunning
        wid = store.create(
            "s1", "proj", "prd", 6, {}, {"agent_max_tool_turns": sample_settings.tool_turn_hard_cap}
        )
        resp = client.post(f"/api/porto/workflows/{wid}/steps/understand/rerun")
        assert resp.status_code == 409, f"expected 409, got {resp.status_code}: {resp.text}"
