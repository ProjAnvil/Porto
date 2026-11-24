from __future__ import annotations

import time

from fastapi.testclient import TestClient

from porto_chatbot import main


def _wait_index_done(client: TestClient, timeout: float = 30.0) -> dict:
    """Helper that waits synchronously for the async reindex: polls /api/kb/stats until the job
    ends."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        stats = client.get("/api/kb/stats").json()
        if stats.get("rag_index", {}).get("status") in ("succeeded", "failed", "interrupted"):
            return stats
        time.sleep(0.15)
    raise AssertionError(f"index did not finish within {timeout}s")


def test_api_chat_and_workflow(monkeypatch, sample_settings, sample_prd):
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        index_resp = client.post("/api/kb/index")
        assert index_resp.status_code == 200
        assert index_resp.json()["status"] == "running"  # async: returns running at once

        stats = _wait_index_done(client)
        assert stats["documents"] == 1
        assert stats["rag_index"]["status"] == "succeeded"

        chat_resp = client.post("/api/chat", json={"message": "支付和风控怎么拆？"})
        assert chat_resp.status_code == 200
        assert "answer" in chat_resp.json()
        assert chat_resp.json()["sources"]

        wf_resp = client.post(
            "/api/porto/workflows",
            json={"text": sample_prd, "project_name": "支付平台"},
        )
        assert wf_resp.status_code == 200
        assert "workflow_id" in wf_resp.json()
        assert wf_resp.json()["status"] == "running"


def test_api_chat_stream_ai_sdk_format(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        _wait_index_done(client)

        with client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "id": "thread-1",
                "session_id": "test-session",
                "messages": [
                    {
                        "id": "msg-1",
                        "role": "user",
                        "parts": [{"type": "text", "text": "支付和风控怎么拆？"}],
                    }
                ],
            },
        ) as response:
            assert response.status_code == 200
            text = "".join(response.iter_text())

    assert 'data: {"type": "start"' in text
    assert 'data: {"type": "text-start"' in text
    assert 'data: {"type": "text-delta"' in text
    assert 'data: {"type": "source-document"' in text
    assert 'data: {"type": "data-porto"' in text
    assert 'data: {"type": "finish"' in text
    assert "data: [DONE]" in text


def test_api_chat_greeting_skips_rag(monkeypatch, sample_settings):
    monkeypatch.setattr(main, "settings", sample_settings)
    client = TestClient(main.app)

    chat_resp = client.post("/api/chat", json={"message": "你好"})

    assert chat_resp.status_code == 200
    data = chat_resp.json()
    assert data["sources"] == []
    assert data["memory"] == []
    assert data["steps"][0]["name"] == "route_intent"
    assert data["steps"][0]["data"]["intent"] == "direct"
    assert all(step["name"] != "retrieve_knowledge" for step in data["steps"])


def test_chat_stream_native_streaming_when_llm_enabled(monkeypatch, sample_settings):
    """With the LLM enabled + agent_stream_enabled, the native token stream is used (several
    text-delta chunks)."""
    from porto_chatbot.agent import langchain_chat_stream as stream_mod
    from porto_chatbot.llm import LLMClient

    sample_settings.agent_api_key = "k"
    sample_settings.agent_stream_enabled = True
    sample_settings.health_probe_timeout = 1  # keeps the lifespan's first health probe from hanging on the external LLM
    monkeypatch.setattr(main, "settings", sample_settings)
    # The intent router always returns rag; stream returns several chunks
    monkeypatch.setattr(LLMClient, "complete_structured", lambda self, *a, **k: {"intent": "rag", "reason": "领域问题"})
    monkeypatch.setattr(LLMClient, "stream", lambda self, *a, **k: iter(["流", "式", "回答"]))
    # The LLM is "enabled" with a fake key, so every LLM entry point on this path must be
    # stubbed or the call goes to the real api.openai.com. `complete` is what
    # maybe_generate_title calls from a daemon thread: unstubbed, the 401 + retry kept that
    # thread alive for seconds and it leaked into later tests (breaking tests that count
    # ChatOpenAI.invoke calls). Stub the trigger as well so no thread outlives this test.
    monkeypatch.setattr(LLMClient, "complete", lambda self, *a, **k: "title")
    monkeypatch.setattr(stream_mod, "maybe_generate_title", lambda *a, **k: None)

    with TestClient(main.app) as client:
        client.post("/api/kb/index")
        _wait_index_done(client)
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "id": "t1",
                "session_id": "s1",
                "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": "支付架构怎么拆"}]}],
            },
        ) as response:
            assert response.status_code == 200
            text = "".join(response.iter_text())

    assert "流" in text and "式" in text and "回答" in text
    assert text.count('"text-delta"') >= 3  # native streaming: at least 3 chunks
    assert "data: [DONE]" in text


# ---------------------------- Context budget (Phase 4 P1) ---------------------------- #


def test_trim_to_budget_under_budget():
    from porto_chatbot.agent.langchain_chat import _trim_to_budget

    parts = ["问题", "摘要", "片段"]
    assert _trim_to_budget(parts, 1000) == ["问题", "摘要", "片段"]


def test_trim_to_budget_trims_from_back():
    from porto_chatbot.agent.langchain_chat import _trim_to_budget

    parts = ["问题", "摘要", "片段" * 100]
    result = _trim_to_budget(parts, 20)
    assert sum(len(p) for p in result) <= 20
    assert "问题" in result[0]  # earlier items are kept first


def test_trim_to_budget_drops_empty_parts():
    from porto_chatbot.agent.langchain_chat import _trim_to_budget

    parts = ["a", "b", "c" * 200]
    result = _trim_to_budget(parts, 5)
    assert all(p for p in result)  # no empty strings left behind
    assert sum(len(p) for p in result) <= 5


def test_trim_to_budget_zero_budget_noop():
    from porto_chatbot.agent.langchain_chat import _trim_to_budget

    parts = ["a", "b"]
    assert _trim_to_budget(parts, 0) == ["a", "b"]


# --------------------------- /api/health snapshot --------------------------- #


def test_api_health_returns_dependency_and_feature_snapshot(monkeypatch, sample_settings):
    """/api/health must return the HealthMonitor snapshot (including dependencies / features).

    Regression: settings.py used to keep a GET /api/health of the same name (returning a
    config overview without dependencies); because it was registered first in app.py it
    overrode the HealthMonitor version in knowledge.py, and the frontend crashed on
    health.dependencies.map. This test locks in the contract "/api/health returns the
    per-dependency health snapshot".
    """
    sample_settings.health_probe_timeout = 1
    monkeypatch.setattr(main, "settings", sample_settings)
    with TestClient(main.app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "dependencies" in data, f"/api/health 缺少 dependencies：{data}"
        assert isinstance(data["dependencies"], list)
        assert "features" in data
        dep_names = {d["name"] for d in data["dependencies"]}
        assert {"embedding", "agent_llm", "critic_llm"} <= dep_names


def test_api_health_probes_db_rag_config_not_env(monkeypatch, sample_settings):
    """/api/health must probe with the effective config from the database, not .env /
    current_settings.

    Regression: HealthMonitor used to be given settings_provider=current_settings (which reads
    .env), so db config changed by the user in the UI (embedding model, agent key) had no
    effect and the panel showed probe results for the old .env values (embedding down / agent
    unknown). After the fix settings_provider should merge the db config before probing.
    """
    from porto_chatbot.api.deps import get_config_store
    from porto_chatbot.models import RagSettingsPayload

    sample_settings.health_probe_timeout = 1
    # The .env source (current_settings) is configured with an unreachable ollama; if health
    # used it, the probe would be down for sure
    sample_settings.embedding_provider = "ollama"
    sample_settings.embedding_base_url = "http://127.0.0.1:1"  # the port refuses connections, failing fast
    monkeypatch.setattr(main, "settings", sample_settings)
    # The db stores local (the probe is ok for sure); the real feature uses the effective
    # config, so it should reflect this entry
    get_config_store().save_rag_settings(RagSettingsPayload(embedding_provider="local"))

    with TestClient(main.app) as client:
        data = client.get("/api/health").json()
    deps = {d["name"]: d for d in data["dependencies"]}
    assert deps["embedding"]["status"] == "ok", (
        f"health 探测应使用 db 配置(local→ok)，实际用了 .env(ollama→down)：{deps['embedding']}"
    )
