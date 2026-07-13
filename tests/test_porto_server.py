"""Tests for porto_server.py."""

import http.client
import json
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

import porto_server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_server_home(porto_home: Path):
    porto_server._set_porto_home(porto_home)


@pytest.fixture()
def server(porto_home):
    """Start a test HTTP server on a random port."""
    _setup_server_home(porto_home)
    # Use port 0 to get a random available port
    srv = HTTPServer(("127.0.0.1", 0), porto_server.PortoHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()
    srv.server_close()
    porto_server._set_porto_home(None)


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    return resp.status, body


def _post(port, path, body_text):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = body_text.encode("utf-8")
    conn.request(
        "POST",
        path,
        body=data,
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Length": str(len(data)),
        },
    )
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    return resp.status, body


# ---------------------------------------------------------------------------
# Tests: _extract_sections
# ---------------------------------------------------------------------------


class TestExtractSections:
    def test_basic_sections(self):
        md = """# Title

## Section One

Some content here.

## Section Two

More content.
"""
        sections = porto_server._extract_sections(md)
        assert len(sections) == 2
        assert sections[0]["title"] == "Section One"
        assert sections[0]["id"] == "section-one"
        assert "Some content" in sections[0]["content_preview"]
        assert sections[1]["title"] == "Section Two"

    def test_empty_markdown(self):
        sections = porto_server._extract_sections("")
        assert sections == []

    def test_no_h2_headings(self):
        md = "# Only H1\n\nParagraph text."
        sections = porto_server._extract_sections(md)
        assert sections == []

    def test_preview_truncation(self):
        md = "## Long Section\n\n" + "x" * 300
        sections = porto_server._extract_sections(md)
        assert len(sections) == 1
        assert sections[0]["content_preview"].endswith("...")
        assert len(sections[0]["content_preview"]) <= 204  # 200 + "..."


# ---------------------------------------------------------------------------
# Tests: _extract_subsystems_from_md
# ---------------------------------------------------------------------------


class TestExtractSubsystems:
    def test_with_typed_subsystems(self):
        md = """# Subsystems

### order-service

| Attribute | Value |
|-----------|-------|
| **Type** | new |
| **Responsibility** | Order management |

### payment-gateway

| Attribute | Value |
|-----------|-------|
| **Type** | extend |
| **Responsibility** | Payment processing |
"""
        subs = porto_server._extract_subsystems_from_md(md)
        assert len(subs) == 2
        assert subs[0]["name"] == "order-service"
        assert subs[0]["type"] == "new"
        assert subs[0]["responsibility"] == "Order management"
        assert subs[1]["name"] == "payment-gateway"
        assert subs[1]["type"] == "extend"

    def test_empty(self):
        subs = porto_server._extract_subsystems_from_md("")
        assert subs == []


# ---------------------------------------------------------------------------
# Tests: _extract_subsystem_specs
# ---------------------------------------------------------------------------


class TestExtractSubsystemSpecs:
    def test_with_specs(self, porto_home):
        step4_dir = porto_home / "workflows" / "test-wf-complete" / "md" / "step4"
        specs = porto_server._extract_subsystem_specs(step4_dir)
        assert len(specs) == 2
        names = [s["name"] for s in specs]
        assert "order-service" in names
        assert "payment-gateway" in names
        for s in specs:
            assert len(s["sections"]) > 0

    def test_nonexistent_dir(self, tmp_path):
        specs = porto_server._extract_subsystem_specs(tmp_path / "nope")
        assert specs == []


# ---------------------------------------------------------------------------
# Tests: _html_escape
# ---------------------------------------------------------------------------


class TestHtmlEscape:
    def test_escapes_special_chars(self):
        assert porto_server._html_escape("<script>\"hello\"&'world'") == (
            "&lt;script&gt;&quot;hello&quot;&amp;&#039;world&#039;"
        )

    def test_empty_string(self):
        assert porto_server._html_escape("") == ""


# ---------------------------------------------------------------------------
# Tests: _list_all_workflows
# ---------------------------------------------------------------------------


class TestListAllWorkflows:
    def test_lists_seed_workflows(self, porto_home):
        _setup_server_home(porto_home)
        workflows = porto_server._list_all_workflows()
        ids = [w["workflow_id"] for w in workflows]
        assert "test-wf-001" in ids
        assert "test-wf-complete" in ids
        assert "test-wf-payment" in ids
        porto_server._set_porto_home(None)

    def test_empty_dir(self, tmp_path):
        home = tmp_path / "empty"
        home.mkdir()
        (home / "workflows").mkdir()
        porto_server._set_porto_home(home)
        workflows = porto_server._list_all_workflows()
        assert workflows == []
        porto_server._set_porto_home(None)


# ---------------------------------------------------------------------------
# Tests: _get_step_md_path
# ---------------------------------------------------------------------------


class TestGetStepMdPath:
    def test_existing_step(self, porto_home):
        wf_dir = porto_home / "workflows" / "test-wf-001"
        p = porto_server._get_step_md_path(wf_dir, 1)
        assert p is not None
        assert p.exists()

    def test_nonexistent_step(self, porto_home):
        wf_dir = porto_home / "workflows" / "test-wf-001"
        p = porto_server._get_step_md_path(wf_dir, 3)
        assert p is None

    def test_step4_returns_none(self, porto_home):
        wf_dir = porto_home / "workflows" / "test-wf-complete"
        # Step 4 is handled separately, not via STEP_MD_FILES
        p = porto_server._get_step_md_path(wf_dir, 4)
        assert p is None


# ---------------------------------------------------------------------------
# Tests: _get_subsystem_spec_path
# ---------------------------------------------------------------------------


class TestGetSubsystemSpecPath:
    def test_existing_spec(self, porto_home):
        wf_dir = porto_home / "workflows" / "test-wf-payment"
        p = porto_server._get_subsystem_spec_path(wf_dir, "risk-engine")
        assert p is not None
        assert p.exists()

    def test_nonexistent_spec(self, porto_home):
        wf_dir = porto_home / "workflows" / "test-wf-payment"
        p = porto_server._get_subsystem_spec_path(wf_dir, "nonexistent")
        assert p is None

    def test_rejects_path_traversal(self, porto_home):
        wf_dir = porto_home / "workflows" / "test-wf-payment"
        assert porto_server._get_subsystem_spec_path(wf_dir, "../../../etc") is None
        assert porto_server._get_subsystem_spec_path(wf_dir, "foo/bar") is None


# ---------------------------------------------------------------------------
# Tests: HTTP API via server fixture
# ---------------------------------------------------------------------------


class TestAPIListWorkflows:
    def test_list(self, server):
        status, body = _get(server, "/api/workflows")
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, list)
        assert len(data) >= 3
        ids = [w["workflow_id"] for w in data]
        assert "test-wf-payment" in ids

    def test_list_has_step_statuses(self, server):
        status, body = _get(server, "/api/workflows")
        data = json.loads(body)
        wf = next(w for w in data if w["workflow_id"] == "test-wf-payment")
        assert wf["completed_steps"] == 4
        assert wf["status"] == "completed"


class TestAPIGetWorkflow:
    def test_existing(self, server):
        status, body = _get(server, "/api/workflows/test-wf-payment")
        assert status == 200
        data = json.loads(body)
        assert data["project_name"] == "互联网支付交易平台"
        assert len(data["steps"]) == 4
        assert data["steps"][0]["has_content"] is True

    def test_nonexistent(self, server):
        status, body = _get(server, "/api/workflows/nope")
        assert status == 404


class TestAPIGetStep:
    def test_step1_returns_markdown(self, server):
        status, body = _get(server, "/api/workflows/test-wf-payment/step/1")
        assert status == 200
        assert "# Step 1" in body

    def test_step4_returns_subsystem_list(self, server):
        status, body = _get(server, "/api/workflows/test-wf-payment/step/4")
        assert status == 200
        data = json.loads(body)
        assert data["step"] == 4
        names = [s["name"] for s in data["subsystems"]]
        assert "payment-core" in names
        assert "risk-engine" in names

    def test_missing_step(self, server):
        status, body = _get(server, "/api/workflows/test-wf-001/step/3")
        assert status == 404

    def test_invalid_step(self, server):
        status, body = _get(server, "/api/workflows/test-wf-payment/step/0")
        assert status == 400


class TestAPIGetSubsystemSpec:
    def test_existing_spec(self, server):
        status, body = _get(server, "/api/workflows/test-wf-payment/step/4/risk-engine")
        assert status == 200
        assert "risk-engine" in body
        assert "执行摘要" in body

    def test_nonexistent_spec(self, server):
        status, body = _get(server, "/api/workflows/test-wf-payment/step/4/nonexistent")
        assert status == 404


class TestAPISaveStep:
    def test_save_step(self, server, porto_home):
        new_content = "# Updated Step 1\n\nNew content here."
        status, body = _post(
            server, "/api/workflows/test-wf-payment/step/1", new_content
        )
        assert status == 200
        data = json.loads(body)
        assert data["ok"] is True
        # Verify file was written
        md_path = (
            porto_home
            / "workflows"
            / "test-wf-payment"
            / "md"
            / "step1_understanding.md"
        )
        assert md_path.read_text(encoding="utf-8") == new_content

    def test_save_nonexistent_workflow(self, server):
        status, body = _post(server, "/api/workflows/nope/step/1", "content")
        assert status == 404


class TestAPISaveSubsystemSpec:
    def test_save_spec(self, server, porto_home):
        new_content = "# Updated risk-engine\n\nNew spec."
        status, body = _post(
            server, "/api/workflows/test-wf-payment/step/4/risk-engine", new_content
        )
        assert status == 200
        data = json.loads(body)
        assert data["ok"] is True
        # Verify
        spec_path = (
            porto_home
            / "workflows"
            / "test-wf-payment"
            / "md"
            / "step4"
            / "risk-engine"
            / "REQUIREMENTS.md"
        )
        assert spec_path.read_text(encoding="utf-8") == new_content

    def test_save_path_traversal(self, server):
        # HTTP client normalizes ../../ in URL paths, so test with encoded dots
        # The handler rejects subsystem names containing ".."
        # We test the function directly instead
        import porto_server as ps

        assert ps._get_subsystem_spec_path(Path("/dummy"), "../etc") is None
        assert ps._get_subsystem_spec_path(Path("/dummy"), "foo/bar") is None


class TestSPARoute:
    def test_root_returns_html(self, server):
        status, body = _get(server, "/")
        assert status == 200
        assert "<!DOCTYPE html>" in body
        assert "Porto Workflow Viewer" in body

    def test_workflow_route_returns_html(self, server):
        status, body = _get(server, "/workflow/test-wf-payment")
        assert status == 200
        assert "<!DOCTYPE html>" in body

    def test_unknown_path_returns_spa(self, server):
        status, body = _get(server, "/anything/else")
        assert status == 200
        assert "<!DOCTYPE html>" in body
