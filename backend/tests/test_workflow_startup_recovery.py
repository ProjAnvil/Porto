"""Task 8: startup recovery marks running workflows as interrupted."""

from __future__ import annotations

from fastapi.testclient import TestClient

from porto_chatbot import main
from porto_chatbot.api.deps import get_workflow_store


def test_startup_marks_running_interrupted(monkeypatch, sample_settings):
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)

    # Phase 1: create a workflow and manually set it to running
    with TestClient(main.app):
        store = get_workflow_store()
        wid = store.create("s", "p", "prd", 6, {}, {})
        store.update_status(wid, "running", current_step="understand")

    # Phase 2: restart app (new lifespan) — should recover running→interrupted
    with TestClient(main.app):
        store = get_workflow_store()
        row = store.get(wid)
        assert row["status"] == "interrupted"
        assert row["current_step"] == "understand"  # must NOT regress
