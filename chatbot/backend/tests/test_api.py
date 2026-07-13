from __future__ import annotations

import time

from fastapi.testclient import TestClient

from porto_chatbot import main


def _wait_index_done(client: TestClient, timeout: float = 30.0) -> dict:
    """异步 reindex 的同步等待 helper：轮询 /api/kb/stats 直到任务结束。"""
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
        assert index_resp.json()["status"] == "running"  # 异步：立即返回 running

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
        data = wf_resp.json()
        assert data["evaluation"]["passed"] is True
        assert any(s["name"] == "payment-service" for s in data["subsystems"])


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
    """LLM enabled + agent_stream_enabled 时走原生 token 流（多个 text-delta 分片）。"""
    from porto_chatbot.llm import LLMClient

    sample_settings.agent_api_key = "k"
    sample_settings.agent_stream_enabled = True
    sample_settings.health_probe_timeout = 1  # 避免 lifespan 首次健康探测连外部 LLM 卡住
    monkeypatch.setattr(main, "settings", sample_settings)
    # intent router 固定返回 rag；stream 返回多个分片
    monkeypatch.setattr(LLMClient, "complete_structured", lambda self, *a, **k: {"intent": "rag", "reason": "领域问题"})
    monkeypatch.setattr(LLMClient, "stream", lambda self, *a, **k: iter(["流", "式", "回答"]))

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
    assert text.count('"text-delta"') >= 3  # 原生流式：至少 3 个分片
    assert "data: [DONE]" in text


# ----------------------------- context 预算（Phase 4 P1）----------------------------- #


def test_trim_to_budget_under_budget():
    from porto_chatbot.api.routes.chat import _trim_to_budget

    parts = ["问题", "摘要", "片段"]
    assert _trim_to_budget(parts, 1000) == ["问题", "摘要", "片段"]


def test_trim_to_budget_trims_from_back():
    from porto_chatbot.api.routes.chat import _trim_to_budget

    parts = ["问题", "摘要", "片段" * 100]
    result = _trim_to_budget(parts, 20)
    assert sum(len(p) for p in result) <= 20
    assert "问题" in result[0]  # 前面优先保留


def test_trim_to_budget_drops_empty_parts():
    from porto_chatbot.api.routes.chat import _trim_to_budget

    parts = ["a", "b", "c" * 200]
    result = _trim_to_budget(parts, 5)
    assert all(p for p in result)  # 无空串残留
    assert sum(len(p) for p in result) <= 5


def test_trim_to_budget_zero_budget_noop():
    from porto_chatbot.api.routes.chat import _trim_to_budget

    parts = ["a", "b"]
    assert _trim_to_budget(parts, 0) == ["a", "b"]


# ----------------------------- /api/health 快照 ----------------------------- #


def test_api_health_returns_dependency_and_feature_snapshot(monkeypatch, sample_settings):
    """/api/health 必须返回 HealthMonitor 快照（含 dependencies/features）。

    回归：settings.py 曾遗留一个同名 GET /api/health（返回配置概览，无 dependencies），
    因在 app.py 中先注册而覆盖了 knowledge.py 的 HealthMonitor 版本，导致前端
    health.dependencies.map 崩溃。此测试锁住「/api/health 返回依赖级健康快照」契约。
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
    """/api/health 探测必须用 db 里的 effective 配置，而非 .env/current_settings。

    回归：HealthMonitor 曾注入 settings_provider=current_settings（读 .env），导致用户
    在 UI 改的 db 配置（embedding 模型、agent key）不生效，面板显示 .env 旧值的探测结果
    （embedding down / agent unknown）。修复后 settings_provider 应合并 db 配置再探测。
    """
    from porto_chatbot.api.deps import get_config_store
    from porto_chatbot.models import RagSettingsPayload

    sample_settings.health_probe_timeout = 1
    # .env 源（current_settings）配成连不上的 ollama —— 若 health 用它，探测必 down
    sample_settings.embedding_provider = "ollama"
    sample_settings.embedding_base_url = "http://127.0.0.1:1"  # 端口拒绝，快速失败
    monkeypatch.setattr(main, "settings", sample_settings)
    # db 存 local（探测必 ok）—— 实际功能走 effective，应反映这条
    get_config_store().save_rag_settings(RagSettingsPayload(embedding_provider="local"))

    with TestClient(main.app) as client:
        data = client.get("/api/health").json()
    deps = {d["name"]: d for d in data["dependencies"]}
    assert deps["embedding"]["status"] == "ok", (
        f"health 探测应使用 db 配置(local→ok)，实际用了 .env(ollama→down)：{deps['embedding']}"
    )
