from __future__ import annotations

from porto_chatbot.models import SessionFact
from porto_chatbot.memory.facts import build_facts_prompt


def _fact(category, content):
    return SessionFact(
        id="x", session_id="s", category=category, content=content,
        source_msg_id="m", created_at="t", updated_at="t",
    )


def test_empty_facts_returns_empty_string():
    assert build_facts_prompt({}) == ""


def test_groups_by_category_with_headers():
    prompt = build_facts_prompt({
        "user_decision": [_fact("user_decision", "登录采用 OAuth")],
        "open_question": [_fact("open_question", "前端框架未定")],
    })
    assert "关键事实" in prompt
    assert "[决策]" in prompt
    assert "[待澄清]" in prompt
    assert "登录采用 OAuth" in prompt
    assert "前端框架未定" in prompt


def test_skips_empty_categories():
    prompt = build_facts_prompt({
        "user_decision": [_fact("user_decision", "X")],
        "open_question": [],  # 空
    })
    assert "[决策]" in prompt
    assert "[待澄清]" not in prompt
