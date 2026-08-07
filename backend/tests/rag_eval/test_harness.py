from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from porto_chatbot.models import SourceChunk
from porto_chatbot.models.enums import QueryTransformStrategy
from porto_chatbot.query_transform import TransformResult
from tests.rag_eval.loaders.domainrag import load_domainrag_from_records
from tests.rag_eval.metrics import THRESHOLDS, aggregate, judge
from tests.rag_eval.provision import build_eval_kb
from tests.rag_eval.runner import run_rag
from tests.rag_eval.schema import CorpusDoc, RagCorpus, RagGolden


def test_schema_constructs():
    corpus = RagCorpus(docs=[CorpusDoc(id="d1", text="你好", metadata={"k": "v"})])
    g = RagGolden(question="Q?", reference_answer="A.", gold_doc_ids=["d1"])
    assert corpus.docs[0].id == "d1"
    assert corpus.docs[0].text == "你好"
    assert g.category == "default"
    assert g.gold_doc_ids == ["d1"]


def _synth_corpus(synth_dir: Path) -> RagCorpus:
    return RagCorpus(
        docs=[
            CorpusDoc(id=p.stem, text=p.read_text(encoding="utf-8"))
            for p in sorted(synth_dir.glob("*.md"))
        ]
    )


def test_build_eval_kb_indexes_and_retrieves(tmp_path):
    synth_dir = Path(__file__).parent / "synth"
    corpus = _synth_corpus(synth_dir)
    kb = build_eval_kb(corpus, tmp_path)
    # 索引就绪
    assert kb.store.is_rag_ready()
    # 已知文档可被中文查询检索命中
    hits = kb.store.search("支付退款结算", top_k=3)
    assert hits, "检索应返回结果"
    assert any("支付" in h.text for h in hits)
    # 隔离：collection 名与默认 porto_kb 不同，data_dir 不在用户家目录
    assert kb.settings.vector_collection == "eval_domainrag"
    assert kb.settings.data_dir != Path.home() / ".porto"


def test_run_rag_assembles_llm_test_case():
    golden = RagGolden(
        question="支付服务负责什么？",
        reference_answer="支付授权、退款、结算与渠道路由。",
        gold_doc_ids=["payment"],
    )
    eval_kb = MagicMock()
    eval_kb.store = MagicMock()
    eval_kb.settings = MagicMock()
    eval_kb.settings.rerank_enabled = False
    chunk = SourceChunk(
        id="c1", path="p", title="t", text="payment-service 负责支付、退款、结算", score=0.9, metadata={}
    )
    eval_kb.store.search.return_value = [chunk]

    llm = MagicMock()
    llm.complete.return_value = "支付服务负责支付授权、退款、结算与渠道路由。"

    tc, result = run_rag(golden, eval_kb, llm, strategy=QueryTransformStrategy.NONE, top_k=3)

    # NONE 策略走 store.search 原路径
    eval_kb.store.search.assert_called_once_with(golden.question, 3)
    assert isinstance(result, TransformResult)
    # LLMTestCase 字段组装正确
    assert tc.input == golden.question
    assert tc.expected_output == golden.reference_answer
    assert tc.actual_output == llm.complete.return_value
    assert tc.retrieval_context == [chunk.text]


def test_run_rag_guards_disabled_llm():
    golden = RagGolden(question="Q", reference_answer="A", gold_doc_ids=["x"])
    eval_kb = MagicMock()
    eval_kb.store = MagicMock()
    eval_kb.settings = MagicMock()
    eval_kb.settings.rerank_enabled = False
    eval_kb.store.search.return_value = []
    llm = MagicMock()
    llm.complete.return_value = None  # LLM 禁用

    tc, _ = run_rag(golden, eval_kb, llm, strategy=QueryTransformStrategy.NONE, top_k=3)
    assert "未返回" in tc.actual_output  # guard 文案
    assert tc.retrieval_context == []


def test_aggregate_means_per_metric():
    per_case = [
        {"faithfulness": 0.8, "answer_relevancy": 0.6},
        {"faithfulness": 0.4, "answer_relevancy": 0.8},
    ]
    agg = aggregate(per_case)
    assert agg["faithfulness"] == 0.6
    assert agg["answer_relevancy"] == 0.7


def test_judge_passes_when_all_above_threshold():
    mean = {k: v + 0.2 for k, v in THRESHOLDS.items()}
    passed, detail = judge(mean)
    assert passed
    assert all(d["passed"] for d in detail.values())


def test_judge_fails_when_any_below_threshold():
    key = next(iter(THRESHOLDS))
    mean = {k: v + 0.2 for k, v in THRESHOLDS.items()}
    mean[key] = THRESHOLDS[key] - 0.1  # 拉低一个
    passed, detail = judge(mean)
    assert not passed
    assert detail[key]["passed"] is False


def test_domainrag_loader_normalizes():
    # 匹配 DomainRAG 真实 schema（int id、answers 为 list of list、positive_reference 带 id）
    corpus_recs = [
        {"id": 159, "title": "中法那些事儿", "url": "u", "contents": "中法学院开设法语课程。"},
    ]
    qa_recs = [
        {
            "question": "用哪三个语言教学？",
            "answers": [["汉语", "法语", "英语"]],
            "positive_reference": [{"id": 159, "title": "中法那些事儿", "contents": "..."}],
        }
    ]
    corpus, goldens = load_domainrag_from_records(corpus_recs, qa_recs)
    assert corpus.docs[0].id == "159"  # int → str
    assert corpus.docs[0].text == "中法学院开设法语课程。"
    assert len(goldens) == 1
    g = goldens[0]
    assert g.question == "用哪三个语言教学？"
    assert g.reference_answer == "汉语、法语、英语"  # join answers[0]
    assert g.gold_doc_ids == ["159"]
