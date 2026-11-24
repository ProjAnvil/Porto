from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from porto_chatbot.models import SourceChunk
from porto_chatbot.models.enums import QueryTransformStrategy
from porto_chatbot.query_transform import TransformResult
from tests.rag_eval.attribution import aggregate_attribution, attribute_answer
from tests.rag_eval.loaders.domainrag import (
    SUBSET_FILES,
    available_subsets,
    load_domainrag_from_records,
)
from tests.rag_eval.loaders.synth import load_synth
from tests.rag_eval.metrics import (
    THRESHOLDS,
    aggregate,
    judge,
    judge_latency,
    latency_summary,
    percentile,
)
from tests.rag_eval.provision import build_eval_kb
from tests.rag_eval.refusal_detector import is_refusal, refusal_verdict
from tests.rag_eval.retrieval_metrics import (
    aggregate_retrieval,
    doc_id_map,
    hit_at_k,
    judge_retrieval,
    mrr,
    noise_ratio,
    recall_at_k,
    retrieval_case_metrics,
    retrieved_doc_ids,
)
from tests.rag_eval.runner import run_rag, run_rag_multiturn
from tests.rag_eval.schema import (
    CorpusDoc,
    EvalProfile,
    GoldenTurn,
    RagCorpus,
    RagGolden,
)


def test_schema_constructs():
    corpus = RagCorpus(docs=[CorpusDoc(id="d1", text="你好", metadata={"k": "v"})])
    g = RagGolden(question="Q?", reference_answer="A.", gold_doc_ids=["d1"])
    assert corpus.docs[0].id == "d1"
    assert corpus.docs[0].text == "你好"
    assert g.category == "default"
    assert g.gold_doc_ids == ["d1"]
    assert g.expected_behavior == "answer"  # answer is the default
    assert g.turns is None  # a single-turn golden has no turns
    assert g.metadata == {}


def test_schema_refusal_and_multiturn():
    r = RagGolden(question="Q", reference_answer="", gold_doc_ids=[],
                  expected_behavior="refuse")
    assert r.expected_behavior == "refuse"
    mt = RagGolden(question="它呢", reference_answer="A2", gold_doc_ids=["d2"],
                   turns=[GoldenTurn("Q1", "A1", ["d1"]), GoldenTurn("它呢", "A2", ["d2"])])
    assert len(mt.turns) == 2
    assert mt.turns[0].gold_doc_ids == ["d1"]


# ── Deterministic retrieval metrics ──────────────────────────────────── #


def _chunks(paths: list[str]) -> list[SourceChunk]:
    from pathlib import PurePosixPath

    return [
        SourceChunk(id=f"c{i}", path=p, title=PurePosixPath(p).stem,
                    text=f"t{i}", score=0.9, metadata={})
        for i, p in enumerate(paths)
    ]


def test_retrieval_metrics_pure_functions():
    gold, retrieved = ["1", "2"], ["9", "1", "3", "2"]
    assert recall_at_k(gold, retrieved) == 1.0
    assert recall_at_k(["1"], ["9", "8"]) == 0.0
    assert mrr(gold, retrieved) == 0.5  # the first gold document ranks 2nd
    assert mrr(gold, ["9", "8"]) == 0.0
    assert hit_at_k(gold, retrieved) == 1.0
    assert hit_at_k(["7"], retrieved) == 0.0
    assert noise_ratio(gold, retrieved) == 0.5  # 2 of 4 are not gold
    # Edge cases: empty gold / empty retrieval
    assert recall_at_k([], retrieved) == 0.0
    assert noise_ratio(gold, []) == 0.0


def test_retrieved_doc_ids_resolves_safe_names():
    corpus = RagCorpus(docs=[CorpusDoc(id="a/b 1", text="x")])
    id_map = doc_id_map(corpus)
    assert id_map["a_b_1"] == "a/b 1"
    chunks = _chunks(["eval_kb/a_b_1.md", "eval_kb/z.md"])
    assert retrieved_doc_ids(chunks, id_map) == ["a/b 1", "z"]


def test_aggregate_and_judge_retrieval():
    per_case = [
        retrieval_case_metrics(["1"], ["1", "x"]),
        retrieval_case_metrics(["2"], ["y", "z"]),
    ]
    mean = aggregate_retrieval(per_case)
    assert mean["recall_at_k"] == 0.5
    assert mean["hit_at_k"] == 0.5
    passed, detail = judge_retrieval(mean)
    # The default threshold is 0.55: recall 0.5 should fail
    assert not passed
    assert detail["recall_at_k"]["passed"] is False


# ── Latency gate ─────────────────────────────────────────────────────── #


def test_percentile_and_latency_judge():
    assert percentile([], 95) == 0.0
    assert percentile([5.0], 95) == 5.0
    vals = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert percentile(vals, 50) == 3.0
    assert percentile(vals, 95) > 80.0  # the large value is captured by p95

    summary = latency_summary([{"retrieve_s": 0.2, "generate_s": 5.0},
                               {"retrieve_s": 0.4, "generate_s": 50.0}])
    assert summary["retrieve_s"]["p95"] == 0.39  # linear interpolation: 0.2 + 0.95×(0.4−0.2)
    ok, detail = judge_latency(summary)
    assert ok  # default thresholds: retrieve 10s / generate 90s
    bad, _ = judge_latency({"generate_s": {"p95": 999.0}})
    assert not bad


# ── Refusal detection ────────────────────────────────────────────────── #


def test_is_refusal_and_verdict():
    assert is_refusal("抱歉，知识库中没有相关信息，无法回答该问题。")
    assert is_refusal("上下文中未提及该内容。")
    # A normal answer that contains "无法" but no refusal phrase → not a refusal (the
    # restrained marker list avoids false positives)
    assert not is_refusal("该专业无法转专业，需在大一末申请。")
    # Refusal expected: refusing scores 1, answering anyway scores 0
    assert refusal_verdict("抱歉，没有相关信息。", "refuse") == 1.0
    assert refusal_verdict("光速约为 299792458 米每秒。", "refuse") == 0.0
    # Answer expected: a normal answer scores 1, refusing by mistake scores 0
    assert refusal_verdict("中法学院用汉语、法语、英语教学。", "answer") == 1.0


def test_is_refusal_long_answer_not_counted():
    # An over-long answer is not a refusal even if it contains a marker (a real refusal does
    # not run on at length)
    assert not is_refusal("无法忽略的是，该专业……" + "细节内容。" * 80)


# ── Claim-level attribution ──────────────────────────────────────────── #


def test_attribute_answer_splits_generation_vs_retrieval_errors():
    answer = "中法学院用三种语言教学。拿破仑出生于科西嘉岛。"
    contexts = ["中法学院 汉语 法语 英语 教学 语言"]
    # Gold not retrieved → unsupported claims are attributed to retrieval_miss
    r1 = attribute_answer(answer, contexts, ["159"], ["100", "101"])
    assert r1["n_unsupported"] >= 1
    assert r1["retrieval_misses"] >= 1 and r1["generation_errors"] == 0
    # Gold already in the retrieved results → unsupported claims are attributed to
    # generation_error
    r2 = attribute_answer(answer, contexts, ["159"], ["159", "100"])
    assert r2["generation_errors"] >= 1 and r2["retrieval_misses"] == 0


def test_aggregate_attribution_rates():
    rows = [
        {"n_claims": 4, "n_unsupported": 2, "generation_errors": 1, "retrieval_misses": 1},
        {"n_claims": 6, "n_unsupported": 1, "generation_errors": 0, "retrieval_misses": 1},
    ]
    agg = aggregate_attribution(rows)
    assert agg["n_claims"] == 10
    assert agg["unsupported_rate"] == 0.3
    assert agg["generation_error_rate"] == 0.1
    assert agg["retrieval_miss_rate"] == 0.2


# ── DomainRAG subset loaders ─────────────────────────────────────────── #


def test_domainrag_registry_covers_seven_subsets():
    subs = available_subsets()
    assert set(subs) == {"extractive", "noisy", "faithful", "multidoc",
                         "structured", "time_sensitive", "conversation"}
    assert len(SUBSET_FILES) == 7


def test_domainrag_loader_normalizes():
    # Matches the real DomainRAG schema (int id, answers as a list of lists,
    # positive_reference with an id)
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


def test_domainrag_subset_variants_normalize():
    corpus_recs = [
        {"id": 1, "title": "表格页", "url": "http://x/t", "contents": "分数线表"},
        {"id": 2, "title": "专业页", "url": "http://x/p", "contents": "专业介绍"},
    ]
    # structured: positive_reference is a dict without an id → look the corpus id up by
    # title
    structured = [{"question": "甘肃分数线？", "answers": [["632"]],
                   "positive_reference": {"title": "表格页", "contents": "…"}}]
    _, gs = load_domainrag_from_records(corpus_recs, structured, subset="structured")
    assert gs[0].gold_doc_ids == ["1"]
    assert gs[0].category == "structured"

    # multidoc: positive_references (plural, a list)
    md = [{"question": "两个专业对比？", "answer": "共同点与差异。",
           "positive_references": [{"id": 1}, {"title": "专业页"}]}]
    _, gs = load_domainrag_from_records(corpus_recs, md, subset="multidoc")
    assert sorted(gs[0].gold_doc_ids) == ["1", "2"]

    # faithful: answers + counterfact metadata
    fa = [{"question": "截止时间？", "answers": [["12月20日"]],
           "counterfact_answers": [["12月19日"]], "commonsense": False,
           "positive_reference": {"id": 2, "title": "专业页"}}]
    _, gs = load_domainrag_from_records(corpus_recs, fa, subset="faithful")
    assert gs[0].gold_doc_ids == ["2"]
    assert gs[0].metadata["counterfact_answers"] == "12月19日"

    # conversation: history_qa + follow-up questions with coreference → turns
    conv = [{
        "history_qa": [{"question": "文学院前身是什么", "answers": ["华北联合大学文艺学院"],
                        "positive_reference": {"title": "表格页"}}],
        "question": "它成立于哪一年", "answers": ["1939年"],
        "positive_reference": {"title": "专业页"},
    }]
    _, gs = load_domainrag_from_records(corpus_recs, conv, subset="conversation")
    assert len(gs[0].turns) == 2
    assert gs[0].turns[0].gold_doc_ids == ["1"]
    assert gs[0].gold_doc_ids == ["2"]  # mirrors the last turn
    assert gs[0].question == "它成立于哪一年"


def test_synth_loader():
    corpus, goldens = load_synth()
    assert len(corpus.docs) == 3
    assert len(goldens) == 2
    assert all(g.gold_doc_ids for g in goldens)


# ── KB build / runner ────────────────────────────────────────────────── #


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
    profile = EvalProfile(name="test_build", max_chunk_chars=600)
    kb = build_eval_kb(corpus, tmp_path, profile)
    # The index is ready
    assert kb.store.is_rag_ready()
    # A known document can be retrieved by a Chinese query
    hits = kb.store.search("支付退款结算", top_k=3)
    assert hits, "检索应返回结果"
    assert any("支付" in h.text for h in hits)
    # The doc id can be recovered from a chunk (the doc-mapping contract of the retrieval
    # track)
    id_map = doc_id_map(corpus)
    ids = retrieved_doc_ids(hits, id_map)
    assert any(i in ids for i in ("payment", "risk", "notify"))
    # Isolation: the collection name is per profile and data_dir is outside the user's home
    # directory
    assert kb.settings.vector_collection == "eval_test_build"
    assert kb.settings.data_dir != Path.home() / ".porto"


def test_build_eval_kb_milvus_backend_injection(tmp_path, monkeypatch):
    """With vector_backend=milvus a Milvus repo is injected (replacing the production storage
    path); the default is chroma.

    The injection chain is verified with a stand-in that records the uri and implements the
    full repo interface on top of chroma; no real Milvus connection.
    """
    from porto_chatbot.vector_stores.chroma import ChromaVectorRepository

    built: dict = {}

    class _FakeMilvusRepo(ChromaVectorRepository):
        def __init__(self, uri, token=None):
            super().__init__(tmp_path / "fake_repo_chroma")
            built["uri"] = uri

    monkeypatch.setattr(
        "porto_chatbot.vector_stores.milvus.MilvusVectorRepository", _FakeMilvusRepo
    )
    corpus = _synth_corpus(Path(__file__).parent / "synth")
    profile = EvalProfile(name="test_milvus", max_chunk_chars=600)

    monkeypatch.setenv("RAG_EVAL_VECTOR_BACKEND", "milvus")
    monkeypatch.setenv("RAG_EVAL_MILVUS_URI", "http://milvus-test:19530")
    kb = build_eval_kb(corpus, tmp_path / "m1", profile, env={})
    assert built["uri"] == "http://milvus-test:19530"
    assert kb.store.is_rag_ready()

    monkeypatch.setenv("RAG_EVAL_VECTOR_BACKEND", "chroma")
    kb2 = build_eval_kb(corpus, tmp_path / "m2", profile, env={})
    assert kb2.store.is_rag_ready()


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
        id="c1", path="eval_kb/payment.md", title="payment",
        text="payment-service 负责支付、退款、结算", score=0.9, metadata={}
    )
    eval_kb.store.search.return_value = [chunk]

    llm = MagicMock()
    llm.complete.return_value = "支付服务负责支付授权、退款、结算与渠道路由。"

    tc, result, timing = run_rag(golden, eval_kb, llm, strategy=QueryTransformStrategy.NONE, top_k=3)

    # The NONE strategy goes through the original store.search path
    eval_kb.store.search.assert_called_once_with(golden.question, 3)
    assert isinstance(result, TransformResult)
    # LLMTestCase fields are assembled correctly
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
    llm.complete.return_value = None  # LLM disabled

    tc, _, _ = run_rag(golden, eval_kb, llm, strategy=QueryTransformStrategy.NONE, top_k=3)
    assert "未返回" in tc.actual_output  # the guard message
    assert tc.retrieval_context == []


def test_run_rag_multiturn_feeds_history():
    golden = RagGolden(
        question="它负责什么？", reference_answer="负责支付。",
        gold_doc_ids=["payment"],
        turns=[GoldenTurn("支付服务是什么？", "是支付系统。", ["payment"]),
               GoldenTurn("它负责什么？", "负责支付。", ["payment"])],
    )
    eval_kb = MagicMock()
    eval_kb.settings = MagicMock()
    eval_kb.settings.rerank_enabled = False
    eval_kb.settings.condense_enabled = False  # this test only checks that history reaches the prompt, not the rewriting
    chunk = SourceChunk(id="c1", path="eval_kb/payment.md", title="payment",
                        text="payment-service 负责支付、退款", score=0.9, metadata={})
    eval_kb.store.search.return_value = [chunk]
    llm = MagicMock()
    llm.complete.side_effect = ["是支付系统。", "负责支付、退款。"]

    mt = run_rag_multiturn(golden, eval_kb, llm, strategy=QueryTransformStrategy.NONE, top_k=3)

    assert len(mt.turns) == 2
    assert mt.timing["n_turns"] == 2
    # The second-turn prompt contains the first turn's history (context for coreference
    # resolution)
    second_user = llm.complete.call_args_list[1].kwargs["user"]
    assert "对话历史" in second_user and "支付服务是什么" in second_user
    # The test_case is assembled from the last turn
    assert mt.test_case.input == "它负责什么？"
    assert mt.test_case.actual_output == "负责支付、退款。"


def test_condense_gate_semantics():
    """Gate node: rewrite only when needed; PASS / no history / empty LLM output all pass the
    question through unchanged."""
    from porto_chatbot.query_transform import condense_question, condense_with_gate

    # Rewrite needed: returns the rewritten query
    llm = MagicMock()
    llm.complete.return_value = "中国人民大学文学院成立于哪一年"
    r = condense_with_gate(
        llm, "它成立于哪一年",
        [("中国人民大学文学院的前身是什么", "华北联合大学文艺学院")],
    )
    assert r.needed is True and r.query == "中国人民大学文学院成立于哪一年"
    assert "对话历史" in llm.complete.call_args.args[1]

    # Already self-contained: the LLM outputs PASS → passed through without rewriting
    llm_pass = MagicMock()
    llm_pass.complete.return_value = "PASS"
    r2 = condense_with_gate(llm_pass, "中国人民大学文学院前身是什么", [("q1", "a1")])
    assert r2.needed is False and r2.query == "中国人民大学文学院前身是什么"

    # No history: passes through at zero cost (no LLM call)
    llm_none = MagicMock()
    r3 = condense_with_gate(llm_none, "任何问题", [])
    assert r3.needed is False and r3.reason == "no_history_or_no_llm"
    llm_none.complete.assert_not_called()

    # fail-open: the LLM returns empty → the original question
    llm_empty = MagicMock()
    llm_empty.complete.return_value = ""
    assert condense_question(llm_empty, "Q", [("a", "b")]) == "Q"


def test_retrieve_with_transform_condense_node_wiring():
    """condense is the first stage of the retrieval pipeline: settings on + history present →
    retrieve only after the rewrite; off → pass through."""
    from porto_chatbot.query_transform import retrieve_with_transform

    store = MagicMock()
    store.search.return_value = []
    settings = MagicMock()
    settings.rerank_enabled = False
    settings.condense_enabled = True
    llm = MagicMock()
    llm.complete.return_value = "支付服务负责什么"  # the gate rewrites

    retrieve_with_transform(
        "它呢", QueryTransformStrategy.NONE, store, settings, llm, 3,
        history=[("支付服务是什么", "支付系统。")],
    )
    # NONE path: the retrieval query is the rewritten one
    assert store.search.call_args.args[0] == "支付服务负责什么"

    # Switch off: no LLM call, the original question is retrieved
    store2 = MagicMock()
    store2.search.return_value = []
    settings_off = MagicMock()
    settings_off.rerank_enabled = False
    settings_off.condense_enabled = False
    llm2 = MagicMock()
    out = retrieve_with_transform(
        "它呢", QueryTransformStrategy.NONE, store2, settings_off, llm2, 3,
        history=[("支付服务是什么", "支付系统。")],
    )
    llm2.complete.assert_not_called()
    assert store2.search.call_args.args[0] == "它呢"
    assert out.condense_applied is False


def test_run_rag_multiturn_condense_via_production_node():
    """The multi-turn runner goes through the production gate node: the second-turn retrieval
    query is rewritten and recorded."""
    golden = RagGolden(
        question="它呢", reference_answer="A2", gold_doc_ids=["d2"],
        turns=[GoldenTurn("支付服务是什么", "支付系统。", ["d1"]),
               GoldenTurn("它呢", "A2", ["d2"])],
    )
    eval_kb = MagicMock()
    eval_kb.settings = MagicMock()
    eval_kb.settings.rerank_enabled = False
    eval_kb.settings.condense_enabled = True  # profile.condense_history is injected by provision
    eval_kb.store.search.return_value = []
    llm3 = MagicMock()
    # Call sequence: turn 1 generation → gate rewrite (turn 2 has history) → turn 2 generation
    llm3.complete.side_effect = [
        "支付系统。",                 # turn 1 generation
        "支付服务负责什么",           # turn 2 gate rewrite (not PASS)
        "负责支付。",                 # turn 2 generation
    ]
    mt = run_rag_multiturn(golden, eval_kb, llm3,
                           strategy=QueryTransformStrategy.NONE, top_k=3)
    # The second-turn retrieval uses the rewritten query (done inside the production node)
    assert eval_kb.store.search.call_args_list[-1].args[0] == "支付服务负责什么"
    assert mt.turns[1].condensed == "支付服务负责什么"
    assert mt.turns[0].condensed == ""  # no history in the first turn, the gate passes through at zero cost


def test_profiles_condense_variants():
    from tests.rag_eval.profiles import PROFILES, get_profile

    for name in ("condense", "condense_rerank", "semantic_embed"):
        assert name in PROFILES
    # Clean A/B: condense and baseline differ only in condense_history
    base = get_profile("baseline")
    cond = get_profile("condense")
    assert cond.condense_history is True and base.condense_history is False
    assert cond.max_chunk_chars == base.max_chunk_chars
    assert cond.rerank_enabled == base.rerank_enabled
    assert cond.strategy == base.strategy


# ── Metric aggregation / verdicts ────────────────────────────────────── #


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
    mean[key] = THRESHOLDS[key] - 0.1  # pull one metric down
    passed, detail = judge(mean)
    assert not passed
    assert detail[key]["passed"] is False
