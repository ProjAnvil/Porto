from __future__ import annotations

from unittest.mock import MagicMock

import porto_chatbot.specs.loop as loop_mod
from porto_chatbot.models import Subsystem
from porto_chatbot.specs.loop import generate_spec_with_loop


def test_loop_skips_critique_when_tool_truncated(monkeypatch):
    tool_meta = {
        "turns": 4,
        "tool_calls": 11,
        "truncated": True,
        "max_turns": 10,
        "reason": "tool_loop_truncated",
    }
    monkeypatch.setattr(
        loop_mod, "generate_initial_spec", lambda ctx, sub: ("⚠️ 规格生成超限", tool_meta)
    )
    ctx = MagicMock()
    ctx.llm.enabled = True
    ctx.settings.spec_refine_enabled = True
    ctx.settings.spec_refine_max_iter = 3
    ctx.settings.spec_refine_pass_score = 10
    ctx.settings.spec_refine_budget_tokens = 40000
    result = generate_spec_with_loop(ctx, Subsystem(name="x", responsibility="r"))
    assert result.tool_meta["truncated"] is True
    assert "超限" in result.final
    ctx.critic_llm.complete_structured.assert_not_called()  # critique 被跳过


def test_loop_normal_carries_tool_meta(monkeypatch):
    tool_meta = {"turns": 2, "tool_calls": 1, "truncated": False, "max_turns": 10, "reason": None}
    monkeypatch.setattr(
        loop_mod, "generate_initial_spec", lambda ctx, sub: ("正常 spec", tool_meta)
    )
    ctx = MagicMock()
    ctx.llm.enabled = True
    ctx.backend = None  # critique 走 backend；None → critique_spec 返回 None → 立即接受
    ctx.settings.spec_refine_enabled = True
    ctx.settings.spec_refine_max_iter = 3
    ctx.settings.spec_refine_pass_score = 10
    ctx.settings.spec_refine_budget_tokens = 40000
    result = generate_spec_with_loop(ctx, Subsystem(name="x", responsibility="r"))
    assert result.tool_meta["truncated"] is False
    assert result.tool_meta["turns"] == 2
