from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest


def _mock_memory() -> MagicMock:
    """返回一个像 MemoryStore 的 mock:get_compacted_history 会读它的 settings。

    必须把 memory_recent_keep / memory_compact_threshold 设成真数字,
    否则 `len(records) <= thresh` 会因 `int <= MagicMock()` 抛 TypeError。
    """
    mem = MagicMock()
    mem.search.return_value = []
    mem.add.return_value = None
    mem.get_messages_ordered.return_value = []
    mem.settings.memory_compact_threshold = 100
    mem.settings.memory_recent_keep = 8
    return mem


def test_chat_injects_facts_into_prompt(monkeypatch, tmp_path):
    """非流式 chat:facts 被注入 prompt_parts(LLM 收到的 user 文本中)。"""
    from porto_chatbot import main
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.memory.facts import SessionFactsStore
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    # chat() 内 apply_rag_settings 通过 current_settings() 读 main.settings
    monkeypatch.setattr(main, "settings", settings)
    MemoryStore(settings)  # 初始化 memory 表 schema

    fs = SessionFactsStore(settings)
    fs.upsert(
        session_id="s1",
        category="user_decision",
        content="登录采用 OAuth",
        source_msg_id="m0",
    )

    captured: dict = {}

    fake_llm = MagicMock()
    fake_llm.enabled = True
    fake_llm.complete.side_effect = lambda system, user, **kw: (
        captured.update(system=system, user=user) or "回答"
    )

    with (
        patch.object(chat_mod, "get_store") as gs,
        patch.object(chat_mod, "get_memory") as gm,
        patch.object(chat_mod, "LLMClient", return_value=fake_llm),
        patch.object(chat_mod, "get_index_supervisor") as gi,
        patch.object(chat_mod, "route_chat_intent") as ri,
        patch.object(chat_mod, "trigger_facts_extraction_sync") as trig_sync,
    ):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]), ensure_index=MagicMock())
        gm.return_value = _mock_memory()
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        req = chat_mod.ChatRequest(message="用 OAuth 吧", session_id="s1")
        chat_mod.chat(req)

    # facts_block 注入到了 LLM 收到的 user prompt
    assert "登录采用 OAuth" in captured["user"]
    assert "关键事实" in captured["user"]

    # 异步提取也被触发(参数对)
    trig_sync.assert_called_once()
    kw = trig_sync.call_args.kwargs
    assert kw["session_id"] == "s1"
    assert kw["new_message"] == "用 OAuth 吧"


def test_chat_stream_triggers_async_extraction(monkeypatch, tmp_path):
    """流式 chat_stream:每轮 user msg 后触发异步提取(不阻塞 SSE)。"""
    from porto_chatbot import main
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(main, "settings", settings)
    MemoryStore(settings)  # 初始化 memory 表 schema

    triggered: dict = {}

    # 真实 trigger_facts_extraction_async 是 sync function(内部 create_task),
    # chat_stream 不会 await 它。所以 mock 也必须是 sync function。
    def fake_trigger(**kw):
        triggered.update(kw)

    fake_llm = MagicMock()
    fake_llm.enabled = True
    fake_llm.stream = MagicMock(return_value=iter(["回答"]))
    fake_llm.complete = MagicMock(return_value="回答")

    with (
        patch.object(chat_mod, "get_store") as gs,
        patch.object(chat_mod, "get_memory") as gm,
        patch.object(chat_mod, "LLMClient", return_value=fake_llm),
        patch.object(chat_mod, "get_index_supervisor") as gi,
        patch.object(chat_mod, "route_chat_intent") as ri,
        patch.object(chat_mod, "trigger_facts_extraction_async", fake_trigger),
    ):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]), ensure_index=MagicMock())
        gm.return_value = _mock_memory()
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        body = {"message": "用 OAuth 吧", "session_id": "s1"}

        async def _drain() -> None:
            # chat_stream 返回 StreamingResponse,内部 events() 才是 async iterator
            response = await chat_mod.chat_stream(body)
            async for _ in response.body_iterator:
                pass

        asyncio.run(_drain())

    assert triggered.get("session_id") == "s1"
    assert "用 OAuth 吧" in triggered.get("new_message", "")


def test_chat_facts_load_fail_open(monkeypatch, tmp_path):
    """facts_store.by_category 抛 OperationalError 时 chat 不报错(fail-open)。

    主 chat 链路读 facts 任何环节失败都不得阻塞响应。这里 monkeypatch by_category
    抛 sqlite3.OperationalError("db locked"),验证 chat() 不抛 + facts_block 为空。
    """
    import sqlite3

    from porto_chatbot import main
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.memory.facts import SessionFactsStore
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path, facts_enabled=True)
    monkeypatch.setattr(main, "settings", settings)
    MemoryStore(settings)  # 初始化 memory 表 schema

    captured: dict = {}

    fake_llm = MagicMock()
    fake_llm.enabled = True
    fake_llm.complete.side_effect = lambda system, user, **kw: (
        captured.update(system=system, user=user) or "回答"
    )

    # 让 by_category 抛 db 锁异常(模拟磁盘满 / db locked / 老 db 缺 migration)
    def _raise(self, sid):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SessionFactsStore, "by_category", _raise)

    with (
        patch.object(chat_mod, "get_store") as gs,
        patch.object(chat_mod, "get_memory") as gm,
        patch.object(chat_mod, "LLMClient", return_value=fake_llm),
        patch.object(chat_mod, "get_index_supervisor") as gi,
        patch.object(chat_mod, "route_chat_intent") as ri,
        patch.object(chat_mod, "trigger_facts_extraction_sync") as trig_sync,
    ):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]), ensure_index=MagicMock())
        gm.return_value = _mock_memory()
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        req = chat_mod.ChatRequest(message="用 OAuth 吧", session_id="s1")
        resp = chat_mod.chat(req)  # 不抛即通过

    assert resp.answer == "回答"
    # facts_block 为空,facts 头 "关键事实" 不应出现在注入给 LLM 的 user 文本里
    assert "关键事实" not in captured["user"]
    # 用户原消息仍然在
    assert "用 OAuth 吧" in captured["user"]
    # trigger 仍被调(内部按 settings.facts_enabled 走,不依赖 facts_block)
    trig_sync.assert_called_once()


@pytest.mark.parametrize("facts_enabled", [True, False])
def test_chat_facts_fail_open_when_empty(monkeypatch, tmp_path, facts_enabled):
    """facts 为空或关闭时 chat 行为不破坏(注入空串,trigger 跳过)。"""
    from porto_chatbot import main
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path, facts_enabled=facts_enabled)
    monkeypatch.setattr(main, "settings", settings)
    MemoryStore(settings)

    captured: dict = {}

    fake_llm = MagicMock()
    fake_llm.enabled = True
    fake_llm.complete.side_effect = lambda system, user, **kw: (
        captured.update(system=system, user=user) or "回答"
    )

    with (
        patch.object(chat_mod, "get_store") as gs,
        patch.object(chat_mod, "get_memory") as gm,
        patch.object(chat_mod, "LLMClient", return_value=fake_llm),
        patch.object(chat_mod, "get_index_supervisor") as gi,
        patch.object(chat_mod, "route_chat_intent") as ri,
        patch.object(chat_mod, "trigger_facts_extraction_sync") as trig_sync,
    ):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]), ensure_index=MagicMock())
        gm.return_value = _mock_memory()
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        req = chat_mod.ChatRequest(message="普通问题", session_id="empty-session")
        chat_mod.chat(req)

    # 不管 facts_enabled,fail-open:不报错,LLM 收到 user 文本
    assert "普通问题" in captured["user"]
    # facts_enabled=False 时 prompt 不应注入 facts 头("关键事实")
    if not facts_enabled:
        assert "关键事实" not in captured["user"]
    # trigger 无论 facts_enabled 都被调一次(内部按 settings.facts_enabled fail-open,
    # Task 7 已覆盖)
    trig_sync.assert_called_once()


def test_chat_stream_facts_load_fail_open(monkeypatch, tmp_path):
    """流式 chat_stream:facts_store.by_category 抛 OperationalError 时不报错(fail-open)。

    镜像 test_chat_facts_load_fail_open 的流式版本。chat_stream 与 chat 的 facts
    读取代码同源(同 try/except + logger.exception 模式),此测试锁住流式路径也遵守
    fail-open 铁律,防止后续重构流式 prompt 拼装时悄悄破坏 fail-open。
    """
    import sqlite3

    from porto_chatbot import main
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.memory.facts import SessionFactsStore
    from porto_chatbot.memory.store import MemoryStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path, facts_enabled=True)
    monkeypatch.setattr(main, "settings", settings)
    MemoryStore(settings)

    captured: dict = {}

    fake_llm = MagicMock()
    fake_llm.enabled = True
    # 流式路径按 agent_stream_enabled 走 stream 或 complete;两个都 mock 以覆盖任一分支
    fake_llm.stream = MagicMock(
        side_effect=lambda system, user, **kw: (
            captured.update(system=system, user=user) or iter(["回答"])
        )
    )
    fake_llm.complete = MagicMock(
        side_effect=lambda system, user, **kw: (
            captured.update(system=system, user=user) or "回答"
        )
    )

    # 让 by_category 抛 db 锁异常(模拟磁盘满 / db locked / 老 db 缺 migration)
    def _raise(self, sid):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SessionFactsStore, "by_category", _raise)

    with (
        patch.object(chat_mod, "get_store") as gs,
        patch.object(chat_mod, "get_memory") as gm,
        patch.object(chat_mod, "LLMClient", return_value=fake_llm),
        patch.object(chat_mod, "get_index_supervisor") as gi,
        patch.object(chat_mod, "route_chat_intent") as ri,
        patch.object(chat_mod, "trigger_facts_extraction_async") as trig_async,
    ):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]), ensure_index=MagicMock())
        gm.return_value = _mock_memory()
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        body = {"message": "用 OAuth 吧", "session_id": "s1"}

        async def _drain() -> None:
            # chat_stream 返回 StreamingResponse,内部 events() 才是 async iterator
            response = await chat_mod.chat_stream(body)
            async for _ in response.body_iterator:
                pass

        asyncio.run(_drain())  # 不抛即通过

    # facts_block 为空,"关键事实" 头不应出现在注入给 LLM 的 user 文本里
    assert "关键事实" not in captured["user"]
    # 用户原消息仍然在
    assert "用 OAuth 吧" in captured["user"]
    # trigger 仍被调(内部按 settings.facts_enabled 走,不依赖 facts_block)
    trig_async.assert_called_once()
