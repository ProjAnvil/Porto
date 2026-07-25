# backend/tests/test_settings_truncation.py
from __future__ import annotations

from porto_chatbot.settings import Settings


def test_default_agent_max_tool_turns_is_10(tmp_path):
    s = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs")
    assert s.agent_max_tool_turns == 10


def test_tool_turn_hard_cap_default_40(tmp_path):
    s = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs")
    assert s.tool_turn_hard_cap == 40


def test_default_agent_max_tokens_is_8000(tmp_path):
    """understand/generate 这类长文步骤需要足够 token 上限;2000 会硬切业务理解报告。"""
    s = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs")
    assert s.agent_max_tokens == 8000


def test_max_output_recovery_attempts_default_2(tmp_path):
    """撞 max_tokens 后的续写轮数(对齐 Qwen adaptive output escalation 第 2 层)。"""
    s = Settings(data_dir=tmp_path, log_dir=tmp_path / "logs")
    assert s.max_output_recovery_attempts == 2
