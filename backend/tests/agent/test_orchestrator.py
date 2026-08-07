# backend/tests/agent/test_orchestrator.py
"""ChatOrchestrator 测试——验证 DIRECT/RAG 路径的持久化行为。"""
from unittest.mock import MagicMock, patch

import pytest

from porto_chatbot.agent.orchestrator import ChatOrchestrator
from porto_chatbot.memory.conversation_memory import ConversationMemory
from porto_chatbot.memory.session_store import SessionStore
from porto_chatbot.models import ChatRequest
from porto_chatbot.models.enums import ChatIntent
from porto_chatbot.vector_store import LocalVectorStore


@pytest.fixture
def orch(sample_settings):
    sessions = SessionStore(sample_settings)
    memory = ConversationMemory(sample_settings)
    kb_store = LocalVectorStore(sample_settings)
    return ChatOrchestrator(sessions, memory, kb_store, sample_settings)


def test_direct_path_persists_sqlite_not_vector(orch, sample_settings):
    """DIRECT 路径：写 SQLite 不写向量库。"""
    req = ChatRequest(message="你好", session_id="test-direct")
    with patch.object(orch, "_route_intent", return_value=MagicMock(
            intent=ChatIntent.DIRECT, reason="greeting")), \
         patch.object(orch, "_llm_complete", return_value="你好！"):
        orch.handle(req)
    msgs = orch.sessions.list_messages("test-direct")
    assert len(msgs) == 2  # user + assistant
    assert orch.memory.count() == 0  # not indexed


def test_rag_path_persists_both(orch, sample_settings):
    """RAG 路径：写 SQLite + 写向量库 + 回填 indexed flag。"""
    req = ChatRequest(message="what is payment", session_id="test-rag")
    with patch.object(orch, "_route_intent", return_value=MagicMock(
            intent=ChatIntent.RAG, reason="keyword")), \
         patch.object(orch, "_check_rag_available", return_value=(True, None)), \
         patch.object(orch, "_llm_complete", return_value="payment service handles it"):
        orch.handle(req)
    msgs = orch.sessions.list_messages("test-rag")
    assert len(msgs) == 2
    assert all(m.indexed for m in msgs)
    assert orch.memory.count() == 2


def test_chitchat_excluded_from_vector_search(orch, sample_settings):
    """先 chitchat，再 RAG——chitchat 不出现在向量检索结果中。"""
    # Turn 1: chitchat
    req1 = ChatRequest(message="你好", session_id="s1")
    with patch.object(orch, "_route_intent", return_value=MagicMock(
            intent=ChatIntent.DIRECT, reason="greeting")), \
         patch.object(orch, "_llm_complete", return_value="你好！"):
        orch.handle(req1)
    # Turn 2: RAG
    req2 = ChatRequest(message="payment", session_id="s1")
    with patch.object(orch, "_route_intent", return_value=MagicMock(
            intent=ChatIntent.RAG, reason="keyword")), \
         patch.object(orch, "_check_rag_available", return_value=(True, None)), \
         patch.object(orch, "_llm_complete", return_value="payment info"):
        orch.handle(req2)
    # Vector search should find payment, not 你好
    results = orch.memory.search("payment", session_id="s1")
    texts = " ".join(r.text.lower() for r in results)
    assert "payment" in texts
    assert "你好" not in texts
    # But session history shows both
    all_msgs = orch.sessions.list_messages("s1")
    assert len(all_msgs) == 4
