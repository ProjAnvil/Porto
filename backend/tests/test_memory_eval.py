from __future__ import annotations

from porto_chatbot.evaluation import evaluate_rag_cases
from porto_chatbot.memory import MemoryStore
from porto_chatbot.models import EvalCase


def test_memory_store_add_list_and_search(sample_settings):
    memory = MemoryStore(sample_settings)
    memory.add(session_id="s1", role="user", content="我关心支付风控和退款流程")
    memory.add(session_id="s1", role="assistant", content="payment-service 需要调用 risk-service")

    items = memory.list_session("s1")
    assert len(items) == 2

    results = memory.search("风险审核", session_id="s1", top_k=2)
    assert results
    assert results[0].path == "memory:s1"


def test_rag_eval_cases_score_context_grounding():
    result = evaluate_rag_cases(
        [
            EvalCase(
                question="支付服务负责什么？",
                answer="支付服务负责支付、退款和结算。",
                contexts=["payment-service handles 支付、退款、结算 and channel routing."],
            )
        ]
    )

    assert result.score > 30
    assert result.cases[0].metrics.groundedness > 0
