"""POST /api/intent/test 决策探测端点。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from porto_chatbot import main
from porto_chatbot.settings import Settings


def _make_client(monkeypatch, tmp_path):
    """test_settings_api.py 的文件局部 client 模式（settings 绑定 tmp_path 隔离实例）。"""
    s = Settings(
        kb_dirs=[tmp_path / "kb"], data_dir=tmp_path / "d", log_dir=tmp_path / "l",
    )
    monkeypatch.setattr(main, "settings", s)
    return TestClient(main.app)


def test_intent_test_rule_engine(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    r = client.post(
        "/api/intent/test",
        json={"message": "你好", "engine": "rule", "routing_mode": "binary"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "direct"
    assert body["reason"] == "greeting"
    assert body["engine"] == "rule"
    assert body["confidence"] is None
    assert body["fallback"] is False
    assert body["latency_ms"] >= 0


def test_intent_test_defaults_to_effective_settings(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    r = client.post("/api/intent/test", json={"message": "帮我查一下支付相关文档"})
    assert r.status_code == 200
    assert r.json()["intent"] in ("rag", "quick_rag", "deep_rag", "direct")


def test_intent_test_rejects_empty_message(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    r = client.post("/api/intent/test", json={"message": ""})
    assert r.status_code == 422
