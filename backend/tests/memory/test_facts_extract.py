from __future__ import annotations

from unittest.mock import MagicMock

from porto_chatbot.models import MemoryRecord, SessionFact
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


# ---------------------------------------------------------------------- #
# Task 6: extract_facts
# ---------------------------------------------------------------------- #


def _make_store(tmp_path):
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.memory.facts import SessionFactsStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    MemoryStore(settings)
    return SessionFactsStore(settings), settings


def _call_extract(store, llm, settings, message):
    from porto_chatbot.memory.facts import extract_facts

    recent = [MemoryRecord(
        id="m1", session_id="s1", role="user", content="做个登录页", created_at="t",
    )]
    return extract_facts(
        store=store, llm=llm, session_id="s1",
        new_message=message, recent_turns=recent, settings=settings,
    )


def test_extract_facts_writes_to_store(tmp_path):
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.return_value = {
        "facts": [
            {"category": "user_decision", "content": "登录采用 OAuth", "action": "add"},
        ]
    }
    n = _call_extract(store, llm, settings, "用 OAuth 吧")
    assert n == 1
    assert len(store.list_active("s1")) == 1


def test_extract_facts_empty_result(tmp_path):
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.return_value = {"facts": []}
    n = _call_extract(store, llm, settings, "你好")
    assert n == 0
    assert store.list_active("s1") == []


def test_extract_facts_llm_disabled(tmp_path):
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = False
    n = _call_extract(store, llm, settings, "用 OAuth 吧")
    assert n == 0
    llm.complete_structured.assert_not_called()


def test_extract_facts_parse_failure_fail_open(tmp_path):
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.return_value = None  # 解析失败
    n = _call_extract(store, llm, settings, "用 OAuth 吧")
    assert n == 0  # fail-open,不抛
    assert store.list_active("s1") == []


def test_extract_facts_retract_action(tmp_path):
    store, settings = _make_store(tmp_path)
    store.upsert(
        session_id="s1", category="user_decision",
        content="登录采用 OAuth", source_msg_id="m0",
    )
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.return_value = {
        "facts": [
            {"category": "user_decision", "content": "登录采用 OAuth", "action": "retract"},
        ]
    }
    n = _call_extract(store, llm, settings, "不用 OAuth 了")
    assert n == 1
    assert store.list_active("s1") == []  # 被 retract


def test_extract_facts_llm_exception_fail_open(tmp_path):
    """complete_structured 抛异常时 fail-open 返回 0,不向上抛。"""
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.side_effect = RuntimeError("LLM timeout")
    n = _call_extract(store, llm, settings, "用 OAuth 吧")
    assert n == 0
    assert store.list_active("s1") == []


# ---------------------------------------------------------------------- #
# Task 7: trigger_facts_extraction_sync / trigger_facts_extraction_async
# ---------------------------------------------------------------------- #

import asyncio  # noqa: E402


def test_trigger_sync_runs_in_thread(tmp_path):
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.return_value = {
        "facts": [{"category": "user_decision", "content": "X", "action": "add"}]
    }
    from porto_chatbot.memory.facts import trigger_facts_extraction_sync
    from porto_chatbot.models import MemoryRecord

    recent = [MemoryRecord(id="m", session_id="s", role="user", content="x", created_at="t")]
    trigger_facts_extraction_sync(
        store=store, llm=llm, session_id="s", new_message="x",
        recent_turns=recent, settings=settings,
    )
    # daemon 线程 fire-and-forget,轮询等待写入完成(避免与线程调度竞争)
    import time
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not store.list_active("s"):
        time.sleep(0.005)
    assert len(store.list_active("s")) == 1


def test_trigger_async_fire_and_forget(tmp_path):
    store, settings = _make_store(tmp_path)
    llm = MagicMock()
    llm.enabled = True
    llm.complete_structured.return_value = {
        "facts": [{"category": "user_decision", "content": "Y", "action": "add"}]
    }
    from porto_chatbot.memory.facts import trigger_facts_extraction_async
    from porto_chatbot.models import MemoryRecord

    recent = [MemoryRecord(id="m", session_id="s", role="user", content="y", created_at="t")]

    async def main():
        task = trigger_facts_extraction_async(
            store=store, llm=llm, session_id="s", new_message="y",
            recent_turns=recent, settings=settings,
        )
        assert task is not None
        await task  # 等异步完成
        assert len(store.list_active("s")) == 1

    asyncio.run(main())


def test_trigger_async_disabled_returns_none(tmp_path):
    store, settings = _make_store(tmp_path)
    settings.facts_enabled = False
    from porto_chatbot.memory.facts import trigger_facts_extraction_async

    task = trigger_facts_extraction_async(
        store=store, llm=MagicMock(), session_id="s", new_message="y",
        recent_turns=[], settings=settings,
    )
    assert task is None  # 直接返回,不创建任务
