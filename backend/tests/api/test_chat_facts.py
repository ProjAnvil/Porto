from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest


def _mock_sessions(settings) -> MagicMock:
    """Return a mock that behaves like a SessionStore: get_compacted_history reads its settings.

    memory_recent_keep / memory_compact_threshold must be real numbers, otherwise
    `len(records) <= thresh` raises TypeError on `int <= MagicMock()`. title is set to a non-None
    value so maybe_generate_title does not start a daemon thread (its LLM call would pollute
    the captured dict).
    """
    sessions = MagicMock()
    sessions.settings = settings
    sessions.get_messages_ordered.return_value = []
    sessions.mark_indexed.return_value = None
    sessions.get_session.return_value = MagicMock(title="existing")
    sessions.update_title.return_value = None
    sessions.ensure_session.return_value = MagicMock(title="existing")
    return sessions


def _mock_conv_memory() -> MagicMock:
    """Return a mock that behaves like a ConversationMemory."""
    mem = MagicMock()
    mem.search.return_value = []
    mem.index.return_value = None
    mem.count.return_value = 0
    return mem


def test_chat_injects_facts_into_prompt(monkeypatch, tmp_path):
    """Non-streaming chat: facts are injected into prompt_parts (inside the user text the LLM
    receives)."""
    from porto_chatbot import main
    from porto_chatbot.agent import langchain_chat as langchain_chat_mod
    from porto_chatbot.agent import orchestrator as orchestrator_mod
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.memory.facts import SessionFactsStore
    from porto_chatbot.memory.session_store import SessionStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    # Inside chat(), apply_rag_settings reads main.settings through current_settings()
    monkeypatch.setattr(main, "settings", settings)
    SessionStore(settings)  # creates the messages + session_facts table schema

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
        patch.object(langchain_chat_mod, "get_store") as gs,
        patch.object(langchain_chat_mod, "get_session_store") as gss,
        patch.object(langchain_chat_mod, "get_conversation_memory") as gcm,
        patch.object(orchestrator_mod, "LLMClient", return_value=fake_llm),
        patch.object(orchestrator_mod, "get_index_supervisor") as gi,
        patch.object(orchestrator_mod, "route_chat_intent") as ri,
        patch.object(orchestrator_mod, "trigger_facts_extraction_sync") as trig_sync,
    ):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]), ensure_index=MagicMock())
        gss.return_value = _mock_sessions(settings)
        gcm.return_value = _mock_conv_memory()
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        req = chat_mod.ChatRequest(message="用 OAuth 吧", session_id="s1")
        chat_mod.chat(req)

    # The facts_block was injected into the user prompt the LLM received
    assert "登录采用 OAuth" in captured["user"]
    assert "关键事实" in captured["user"]

    # Async extraction was triggered too (with the right arguments)
    trig_sync.assert_called_once()
    kw = trig_sync.call_args.kwargs
    assert kw["session_id"] == "s1"
    assert kw["new_message"] == "用 OAuth 吧"


def test_chat_stream_triggers_async_extraction(monkeypatch, tmp_path):
    """Streaming chat_stream: async extraction is triggered after each user message (without
    blocking the SSE stream)."""
    from porto_chatbot import main
    from porto_chatbot.agent import langchain_chat as langchain_chat_mod
    from porto_chatbot.agent import langchain_chat_stream as stream_mod
    from porto_chatbot.agent import orchestrator as orchestrator_mod
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.memory.session_store import SessionStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(main, "settings", settings)
    SessionStore(settings)  # creates the messages + session_facts table schema

    triggered: dict = {}

    # The real trigger_facts_extraction_async is a sync function (it calls create_task inside)
    # and chat_stream does not await it, so the mock has to be a sync function too.
    def fake_trigger(**kw):
        triggered.update(kw)

    fake_llm = MagicMock()
    fake_llm.enabled = True
    fake_llm.stream = MagicMock(return_value=iter(["回答"]))
    fake_llm.complete = MagicMock(return_value="回答")

    with (
        patch.object(langchain_chat_mod, "get_store") as gs,
        patch.object(langchain_chat_mod, "get_session_store") as gss,
        patch.object(langchain_chat_mod, "get_conversation_memory") as gcm,
        patch.object(orchestrator_mod, "LLMClient", return_value=fake_llm),
        patch.object(orchestrator_mod, "get_index_supervisor") as gi,
        patch.object(orchestrator_mod, "route_chat_intent") as ri,
        patch.object(stream_mod, "trigger_facts_extraction_async", fake_trigger),
    ):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]), ensure_index=MagicMock())
        gss.return_value = _mock_sessions(settings)
        gcm.return_value = _mock_conv_memory()
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        body = {"message": "用 OAuth 吧", "session_id": "s1"}

        async def _drain() -> None:
            # chat_stream returns a StreamingResponse; its inner events() is the async iterator
            response = await chat_mod.chat_stream(body)
            async for _ in response.body_iterator:
                pass

        asyncio.run(_drain())

    assert triggered.get("session_id") == "s1"
    assert "用 OAuth 吧" in triggered.get("new_message", "")


def test_chat_facts_load_fail_open(monkeypatch, tmp_path):
    """When facts_store.by_category raises OperationalError, chat does not fail (fail-open).

    A failure anywhere in the facts read must never block the main chat path from responding.
    Here by_category is monkeypatched to raise sqlite3.OperationalError("db locked"); check that
    chat() does not raise and the facts_block is empty.
    """
    import sqlite3

    from porto_chatbot import main
    from porto_chatbot.agent import langchain_chat as langchain_chat_mod
    from porto_chatbot.agent import orchestrator as orchestrator_mod
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.memory.facts import SessionFactsStore
    from porto_chatbot.memory.session_store import SessionStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path, facts_enabled=True)
    monkeypatch.setattr(main, "settings", settings)
    SessionStore(settings)  # creates the messages + session_facts table schema

    captured: dict = {}

    fake_llm = MagicMock()
    fake_llm.enabled = True
    fake_llm.complete.side_effect = lambda system, user, **kw: (
        captured.update(system=system, user=user) or "回答"
    )

    # Make by_category raise a db lock error (simulating a full disk / db locked / an old db
    # missing the migration)
    def _raise(self, sid):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SessionFactsStore, "by_category", _raise)

    with (
        patch.object(langchain_chat_mod, "get_store") as gs,
        patch.object(langchain_chat_mod, "get_session_store") as gss,
        patch.object(langchain_chat_mod, "get_conversation_memory") as gcm,
        patch.object(orchestrator_mod, "LLMClient", return_value=fake_llm),
        patch.object(orchestrator_mod, "get_index_supervisor") as gi,
        patch.object(orchestrator_mod, "route_chat_intent") as ri,
        patch.object(orchestrator_mod, "trigger_facts_extraction_sync") as trig_sync,
    ):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]), ensure_index=MagicMock())
        gss.return_value = _mock_sessions(settings)
        gcm.return_value = _mock_conv_memory()
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        req = chat_mod.ChatRequest(message="用 OAuth 吧", session_id="s1")
        resp = chat_mod.chat(req)  # passing means it did not raise

    assert resp.answer == "回答"
    # The facts_block is empty, so the facts header "关键事实" must not appear in the user
    # text injected for the LLM
    assert "关键事实" not in captured["user"]
    # The user's original message is still there
    assert "用 OAuth 吧" in captured["user"]
    # The trigger is still called (it follows settings.facts_enabled internally and does not
    # depend on the facts_block)
    trig_sync.assert_called_once()


@pytest.mark.parametrize("facts_enabled", [True, False])
def test_chat_facts_fail_open_when_empty(monkeypatch, tmp_path, facts_enabled):
    """With empty or disabled facts the chat behavior stays intact (an empty string is injected
    and the trigger is skipped)."""
    from porto_chatbot import main
    from porto_chatbot.agent import langchain_chat as langchain_chat_mod
    from porto_chatbot.agent import orchestrator as orchestrator_mod
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.memory.session_store import SessionStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path, facts_enabled=facts_enabled)
    monkeypatch.setattr(main, "settings", settings)
    SessionStore(settings)

    captured: dict = {}

    fake_llm = MagicMock()
    fake_llm.enabled = True
    fake_llm.complete.side_effect = lambda system, user, **kw: (
        captured.update(system=system, user=user) or "回答"
    )

    with (
        patch.object(langchain_chat_mod, "get_store") as gs,
        patch.object(langchain_chat_mod, "get_session_store") as gss,
        patch.object(langchain_chat_mod, "get_conversation_memory") as gcm,
        patch.object(orchestrator_mod, "LLMClient", return_value=fake_llm),
        patch.object(orchestrator_mod, "get_index_supervisor") as gi,
        patch.object(orchestrator_mod, "route_chat_intent") as ri,
        patch.object(orchestrator_mod, "trigger_facts_extraction_sync") as trig_sync,
    ):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]), ensure_index=MagicMock())
        gss.return_value = _mock_sessions(settings)
        gcm.return_value = _mock_conv_memory()
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        req = chat_mod.ChatRequest(message="普通问题", session_id="empty-session")
        chat_mod.chat(req)

    # Regardless of facts_enabled it fails open: no error, and the LLM receives the user text
    assert "普通问题" in captured["user"]
    # With facts_enabled=False the prompt must not get the facts header ("关键事实")
    if not facts_enabled:
        assert "关键事实" not in captured["user"]
    # The trigger is called exactly once regardless of facts_enabled (it fails open internally
    # based on settings.facts_enabled, covered by Task 7)
    trig_sync.assert_called_once()


def test_chat_stream_facts_load_fail_open(monkeypatch, tmp_path):
    """Streaming chat_stream: when facts_store.by_category raises OperationalError there is no
    error (fail-open).

    Streaming mirror of test_chat_facts_load_fail_open. chat_stream and chat share the facts
    read code (same try/except + logger.exception pattern); this test locks in that the
    streaming path also follows the fail-open rule, so a later refactor of the streaming prompt
    assembly cannot quietly break it.
    """
    import sqlite3

    from porto_chatbot import main
    from porto_chatbot.agent import langchain_chat as langchain_chat_mod
    from porto_chatbot.agent import langchain_chat_stream as stream_mod
    from porto_chatbot.agent import orchestrator as orchestrator_mod
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.memory.facts import SessionFactsStore
    from porto_chatbot.memory.session_store import SessionStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path, facts_enabled=True)
    monkeypatch.setattr(main, "settings", settings)
    SessionStore(settings)

    captured: dict = {}

    fake_llm = MagicMock()
    fake_llm.enabled = True
    # The streaming path uses stream or complete depending on agent_stream_enabled; both are
    # mocked to cover either branch
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

    # Make by_category raise a db lock error (simulating a full disk / db locked / an old db
    # missing the migration)
    def _raise(self, sid):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SessionFactsStore, "by_category", _raise)

    with (
        patch.object(langchain_chat_mod, "get_store") as gs,
        patch.object(langchain_chat_mod, "get_session_store") as gss,
        patch.object(langchain_chat_mod, "get_conversation_memory") as gcm,
        patch.object(orchestrator_mod, "LLMClient", return_value=fake_llm),
        patch.object(orchestrator_mod, "get_index_supervisor") as gi,
        patch.object(orchestrator_mod, "route_chat_intent") as ri,
        patch.object(stream_mod, "trigger_facts_extraction_async") as trig_async,
    ):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]), ensure_index=MagicMock())
        gss.return_value = _mock_sessions(settings)
        gcm.return_value = _mock_conv_memory()
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        body = {"message": "用 OAuth 吧", "session_id": "s1"}

        async def _drain() -> None:
            # chat_stream returns a StreamingResponse; its inner events() is the async iterator
            response = await chat_mod.chat_stream(body)
            async for _ in response.body_iterator:
                pass

        asyncio.run(_drain())  # passing means it did not raise

    # The facts_block is empty, so the facts header "关键事实" must not appear in the user
    # text injected for the LLM
    assert "关键事实" not in captured["user"]
    # The user's original message is still there
    assert "用 OAuth 吧" in captured["user"]
    # The trigger is still called (it follows settings.facts_enabled internally and does not
    # depend on the facts_block)
    trig_async.assert_called_once()


def test_chat_propagates_transform_degraded(monkeypatch, tmp_path):
    """Task 8: when retrieve_with_transform returns degraded, ChatResponse.transform_degraded is
    non-empty.

    The default binary+none path goes retrieve_with_transform(NONE) → store.search with
    degraded=False (transform_degraded=None, for backward compatibility). This test
    monkeypatches retrieve_with_transform to force degraded=True and asserts that
    ChatResponse.transform_degraded passes the degradation reason through.
    """
    from porto_chatbot import main
    from porto_chatbot.agent import langchain_chat as langchain_chat_mod
    from porto_chatbot.agent import orchestrator as orchestrator_mod
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.memory.session_store import SessionStore
    from porto_chatbot.query_transform import TransformResult
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(main, "settings", settings)
    SessionStore(settings)  # creates the schema

    fake_llm = MagicMock()
    fake_llm.enabled = True
    fake_llm.complete = MagicMock(return_value="回答")

    degraded_result = TransformResult([], degraded=True, degrade_reason="llm_call_failed")

    with (
        patch.object(
            orchestrator_mod, "retrieve_with_transform", return_value=degraded_result
        ) as grt,
        patch.object(langchain_chat_mod, "get_store") as gs,
        patch.object(langchain_chat_mod, "get_session_store") as gss,
        patch.object(langchain_chat_mod, "get_conversation_memory") as gcm,
        patch.object(orchestrator_mod, "LLMClient", return_value=fake_llm),
        patch.object(orchestrator_mod, "get_index_supervisor") as gi,
        patch.object(orchestrator_mod, "route_chat_intent") as ri,
        patch.object(orchestrator_mod, "trigger_facts_extraction_sync"),
    ):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]), ensure_index=MagicMock())
        gss.return_value = _mock_sessions(settings)
        gcm.return_value = _mock_conv_memory()
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        req = chat_mod.ChatRequest(message="查知识库", session_id="s1")
        resp = chat_mod.chat(req)

    # transform_degraded passes the degradation reason string through
    assert resp.transform_degraded == "llm_call_failed"
    # retrieve_with_transform was called (the default binary + rag intent takes the transform
    # branch)
    grt.assert_called_once()


def test_chat_transform_degraded_none_when_normal(monkeypatch, tmp_path):
    """Task 8: default binary+none + normal retrieval → transform_degraded=None (locks in
    backward compatibility).

    retrieve_with_transform is not mocked: the real retrieve_with_transform(NONE) →
    store.search path runs, degraded is always False, and ChatResponse.transform_degraded must
    be None.
    """
    from porto_chatbot import main
    from porto_chatbot.agent import langchain_chat as langchain_chat_mod
    from porto_chatbot.agent import orchestrator as orchestrator_mod
    from porto_chatbot.api.routes import chat as chat_mod
    from porto_chatbot.memory.session_store import SessionStore
    from porto_chatbot.settings import Settings

    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(main, "settings", settings)
    SessionStore(settings)

    fake_llm = MagicMock()
    fake_llm.enabled = True
    fake_llm.complete = MagicMock(return_value="回答")

    with (
        patch.object(langchain_chat_mod, "get_store") as gs,
        patch.object(langchain_chat_mod, "get_session_store") as gss,
        patch.object(langchain_chat_mod, "get_conversation_memory") as gcm,
        patch.object(orchestrator_mod, "LLMClient", return_value=fake_llm),
        patch.object(orchestrator_mod, "get_index_supervisor") as gi,
        patch.object(orchestrator_mod, "route_chat_intent") as ri,
        patch.object(orchestrator_mod, "trigger_facts_extraction_sync"),
    ):
        gs.return_value = MagicMock(search=MagicMock(return_value=[]), ensure_index=MagicMock())
        gss.return_value = _mock_sessions(settings)
        gcm.return_value = _mock_conv_memory()
        gi.return_value.rag_available.return_value = (True, "")
        ri.return_value = MagicMock(intent="rag", reason="x")

        req = chat_mod.ChatRequest(message="普通问题", session_id="s1")
        resp = chat_mod.chat(req)

    # On the default binary+none path transform_degraded must be None (backward compatibility)
    assert resp.transform_degraded is None
