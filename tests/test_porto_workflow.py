"""Tests for porto_workflow.py."""

import json
from pathlib import Path

import porto_workflow
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_workflow_home(porto_home: Path):
    """Point porto_workflow at our test home."""
    porto_workflow._set_porto_home(porto_home)


def _run_init(porto_home: Path, input_file: Path, project: str = "TestProj") -> dict:
    """Helper to run cmd_init and capture JSON output."""
    _setup_workflow_home(porto_home)

    class Args:
        inputs = str(input_file)
        project = None

    args = Args()
    args.project = project

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        porto_workflow.cmd_init(args)
    return json.loads(buf.getvalue())


# ---------------------------------------------------------------------------
# Tests: _get_porto_home / _set_porto_home
# ---------------------------------------------------------------------------


class TestPortoHome:
    def test_set_and_get(self, tmp_path):
        porto_workflow._set_porto_home(tmp_path / "custom")
        assert porto_workflow._get_porto_home() == tmp_path / "custom"

    def test_env_var(self, tmp_path, monkeypatch):
        porto_workflow._set_porto_home(None)  # reset
        monkeypatch.setenv("PORTO_HOME", str(tmp_path / "env"))
        assert porto_workflow._get_porto_home() == tmp_path / "env"

    def test_default(self, monkeypatch):
        porto_workflow._set_porto_home(None)
        monkeypatch.delenv("PORTO_HOME", raising=False)
        assert porto_workflow._get_porto_home() == Path.home() / ".porto"

    def teardown_method(self):
        porto_workflow._set_porto_home(None)


# ---------------------------------------------------------------------------
# Tests: cmd_init
# ---------------------------------------------------------------------------


class TestCmdInit:
    def test_creates_workflow(self, porto_home, tmp_path):
        input_file = tmp_path / "req.md"
        input_file.write_text("# My Requirements")

        result = _run_init(porto_home, input_file)

        assert result["status"] == "initialized"
        assert result["project_name"] == "TestProj"
        assert result["current_step"] == 1
        assert "workflow_id" in result

        # Verify files on disk
        wf_dir = porto_home / "workflows" / result["workflow_id"]
        assert wf_dir.exists()
        assert (wf_dir / "workflow.json").exists()
        assert (wf_dir / "inputs" / "req.md").exists()
        assert (wf_dir / "md").exists()
        assert (wf_dir / "current_step").read_text().strip() == "1"

    def test_missing_input_exits(self, porto_home, tmp_path):
        _setup_workflow_home(porto_home)

        class Args:
            inputs = str(tmp_path / "nonexistent.md")
            project = "Test"

        with pytest.raises(SystemExit):
            porto_workflow.cmd_init(Args())

    def teardown_method(self):
        porto_workflow._set_porto_home(None)


# ---------------------------------------------------------------------------
# Tests: load_workflow / save_workflow / resolve_workflow_id
# ---------------------------------------------------------------------------


class TestWorkflowIO:
    def test_load_existing(self, porto_home):
        _setup_workflow_home(porto_home)
        state = porto_workflow.load_workflow("test-wf-001")
        assert state["workflow_id"] == "test-wf-001"
        assert state["project_name"] == "Test Project"

    def test_load_missing_exits(self, porto_home):
        _setup_workflow_home(porto_home)
        with pytest.raises(SystemExit):
            porto_workflow.load_workflow("nonexistent-id")

    def test_save_and_reload(self, porto_home):
        _setup_workflow_home(porto_home)
        state = porto_workflow.load_workflow("test-wf-001")
        state["project_name"] = "Updated Name"
        porto_workflow.save_workflow(state)

        reloaded = porto_workflow.load_workflow("test-wf-001")
        assert reloaded["project_name"] == "Updated Name"

    def test_resolve_full_id(self, porto_home):
        _setup_workflow_home(porto_home)
        assert porto_workflow.resolve_workflow_id("test-wf-001") == "test-wf-001"

    def test_resolve_short_id(self, porto_home):
        _setup_workflow_home(porto_home)
        resolved = porto_workflow.resolve_workflow_id("test-wf-0")
        assert resolved == "test-wf-001"

    def test_resolve_nonexistent(self, porto_home):
        _setup_workflow_home(porto_home)
        assert porto_workflow.resolve_workflow_id("xxxxxxxxx") is None

    def teardown_method(self):
        porto_workflow._set_porto_home(None)


# ---------------------------------------------------------------------------
# Tests: find_active_workflow
# ---------------------------------------------------------------------------


class TestFindActiveWorkflow:
    def test_finds_in_progress(self, porto_home):
        _setup_workflow_home(porto_home)
        active = porto_workflow.find_active_workflow()
        assert active is not None
        assert active["status"] == "in_progress"
        assert active["workflow_id"] == "test-wf-001"

    def test_returns_none_when_empty(self, tmp_path):
        home = tmp_path / "empty_home"
        home.mkdir()
        (home / "workflows").mkdir()
        porto_workflow._set_porto_home(home)
        assert porto_workflow.find_active_workflow() is None

    def teardown_method(self):
        porto_workflow._set_porto_home(None)


# ---------------------------------------------------------------------------
# Tests: step operations (step-start, step-complete, step-fail)
# ---------------------------------------------------------------------------


class TestStepOperations:
    def _make_args(self, **kwargs):
        class Args:
            pass

        a = Args()
        for k, v in kwargs.items():
            setattr(a, k, v)
        return a

    def test_step_start(self, porto_home):
        _setup_workflow_home(porto_home)

        import io
        from contextlib import redirect_stdout

        args = self._make_args(workflow="test-wf-001", step=3)
        buf = io.StringIO()
        with redirect_stdout(buf):
            porto_workflow.cmd_step_start(args)
        result = json.loads(buf.getvalue())
        assert result["status"] == "step_started"
        assert result["step"] == 3

    def test_step_complete(self, porto_home):
        _setup_workflow_home(porto_home)

        import io
        from contextlib import redirect_stdout

        args = self._make_args(
            workflow="test-wf-001",
            step=3,
            output="md/step3_context.md",
            summary=None,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            porto_workflow.cmd_step_complete(args)
        result = json.loads(buf.getvalue())
        assert result["status"] == "step_completed"
        assert result["step"] == 3

    def test_step_fail(self, porto_home):
        _setup_workflow_home(porto_home)

        import io
        from contextlib import redirect_stdout

        args = self._make_args(
            workflow="test-wf-001",
            step=3,
            error="Test failure",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            porto_workflow.cmd_step_fail(args)
        result = json.loads(buf.getvalue())
        assert result["status"] == "step_failed"
        assert result["error"] == "Test failure"

    def test_complete_already_completed_exits(self, porto_home):
        _setup_workflow_home(porto_home)
        # Step 1 is already completed
        args = self._make_args(workflow="test-wf-001", step=1)
        with pytest.raises(SystemExit):
            porto_workflow.cmd_step_start(args)

    def teardown_method(self):
        porto_workflow._set_porto_home(None)


# ---------------------------------------------------------------------------
# Tests: cmd_advance
# ---------------------------------------------------------------------------


class TestCmdAdvance:
    def _make_args(self, **kwargs):
        class Args:
            pass

        a = Args()
        for k, v in kwargs.items():
            setattr(a, k, v)
        return a

    def test_advance_after_complete(self, porto_home):
        _setup_workflow_home(porto_home)

        import io
        from contextlib import redirect_stdout

        # First complete step 3
        args = self._make_args(
            workflow="test-wf-001",
            step=3,
            output="md/step3_context.md",
            summary=None,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            porto_workflow.cmd_step_complete(args)

        # Then advance
        args = self._make_args(workflow="test-wf-001")
        buf = io.StringIO()
        with redirect_stdout(buf):
            porto_workflow.cmd_advance(args)
        result = json.loads(buf.getvalue())
        assert result["status"] == "advanced"
        assert result["from_step"] == 3
        assert result["to_step"] == 4

    def test_advance_without_complete_exits(self, porto_home):
        _setup_workflow_home(porto_home)
        # Step 3 is in_progress, not completed
        args = self._make_args(workflow="test-wf-001")
        with pytest.raises(SystemExit):
            porto_workflow.cmd_advance(args)

    def teardown_method(self):
        porto_workflow._set_porto_home(None)


# ---------------------------------------------------------------------------
# Tests: STEP_DEFINITIONS / md/ prefix
# ---------------------------------------------------------------------------


class TestStepDefinitions:
    def test_all_steps_have_md_prefix(self):
        for step_num, defn in porto_workflow.STEP_DEFINITIONS.items():
            output = defn.get("output_file") or defn.get("output_dir")
            assert output.startswith(
                "md/"
            ), f"Step {step_num} output '{output}' does not start with 'md/'"

    def test_total_steps_is_4(self):
        assert porto_workflow.TOTAL_STEPS == 4
