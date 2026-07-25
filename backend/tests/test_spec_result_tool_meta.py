from __future__ import annotations
from porto_chatbot.models import SpecResult


def test_tool_meta_defaults_empty():
    r = SpecResult(final="x")
    assert r.tool_meta == {}


def test_tool_meta_carries_truncation_info():
    r = SpecResult(final="x", tool_meta={"turns": 4, "truncated": True})
    assert r.tool_meta["truncated"] is True
    assert r.model_dump()["tool_meta"]["turns"] == 4
