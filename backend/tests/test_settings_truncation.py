# backend/tests/test_settings_truncation.py
from __future__ import annotations

from porto_chatbot.settings import Settings


def test_default_agent_max_tool_turns_is_10(tmp_path):
    s = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs")
    assert s.agent_max_tool_turns == 10


def test_tool_turn_hard_cap_default_40(tmp_path):
    s = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs")
    assert s.tool_turn_hard_cap == 40
