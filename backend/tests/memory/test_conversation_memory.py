"""ConversationMemory 单元测试——纯 ChromaDB 向量操作。"""
from porto_chatbot.memory.conversation_memory import ConversationMemory
from porto_chatbot.models import MessageRecord


def _msg(session_id="s1", role="user", content="hello", intent="rag"):
    return MessageRecord(
        id=f"m-{content}", session_id=session_id, role=role, content=content,
        intent=intent, created_at="2026-01-01T00:00:00Z",
    )


def test_index_and_search_roundtrip(sample_settings):
    mem = ConversationMemory(sample_settings)
    mem.index([_msg(content="payment platform architecture")])
    # Search with the same content — local embeddings guarantee self-match
    results = mem.search("payment platform architecture", session_id="s1", top_k=5)
    assert len(results) >= 1
    assert "payment" in results[0].text.lower()


def test_session_isolation(sample_settings):
    mem = ConversationMemory(sample_settings)
    mem.index([_msg(session_id="A", content="alpha topic")])
    mem.index([_msg(session_id="B", content="beta topic")])
    results_a = mem.search("alpha", session_id="A")
    results_b = mem.search("alpha", session_id="B")
    assert any("alpha" in r.text.lower() for r in results_a)
    assert not any("alpha" in r.text.lower() for r in results_b)


def test_count(sample_settings):
    mem = ConversationMemory(sample_settings)
    assert mem.count() == 0
    mem.index([_msg(content="x"), _msg(content="y")])
    assert mem.count() == 2
    assert mem.count(session_id="s1") == 2
    assert mem.count(session_id="other") == 0


def test_reset(sample_settings):
    mem = ConversationMemory(sample_settings)
    mem.index([_msg(content="x")])
    assert mem.count() == 1
    mem.reset()
    assert mem.count() == 0


def test_empty_collection_search_returns_empty(sample_settings):
    mem = ConversationMemory(sample_settings)
    results = mem.search("anything", session_id="s1")
    assert results == []
