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
    assert g.expected_behavior == "answer"  # 缺省应答
    assert g.turns is None  # 单轮 golden 无轮次
    assert g.metadata == {}


def test_schema_refusal_and_multiturn():
    r = RagGolden(question="Q", reference_answer="", gold_doc_ids=[],
                  expected_behavior="refuse")
    assert r.expected_behavior == "refuse"
    mt = RagGolden(question="它呢", reference_answer="A2", gold_doc_ids=["d2"],
                   turns=[GoldenTurn("Q1", "A1", ["d1"]), GoldenTurn("它呢", "A2", ["d2"])])
    assert len(mt.turns) == 2
    assert mt.turns[0].gold_doc_ids == ["d1"]


# ── 确定性检索指标 ─────────────────────────────────────────────────────── #


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
    assert mrr(gold, retrieved) == 0.5  # 首个 gold 排第 2
    assert mrr(gold, ["9", "8"]) == 0.0
    assert hit_at_k(gold, retrieved) == 1.0
    assert hit_at_k(["7"], retrieved) == 0.0
    assert noise_ratio(gold, retrieved) == 0.5  # 2/4 非 gold
    # 空 gold / 空检索的边界
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
    # 阈值默认 0.55：recall 0.5 应 fail
    assert not passed
    assert detail["recall_at_k"]["passed"] is False


# ── 延迟门禁 ───────────────────────────────────────────────────────────── #


def test_percentile_and_latency_judge():
    assert percentile([], 95) == 0.0
    assert percentile([5.0], 95) == 5.0
    vals = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert percentile(vals, 50) == 3.0
    assert percentile(vals, 95) > 80.0  # 大值被 p95 捕获

    summary = latency_summary([{"retrieve_s": 0.2, "generate_s": 5.0},
                               {"retrieve_s": 0.4, "generate_s": 50.0}])
    assert summary["retrieve_s"]["p95"] == 0.39  # 线性插值：0.2 + 0.95×(0.4−0.2)
    ok, detail = judge_latency(summary)
    assert ok  # 默认阈值 retrieve 10s / generate 90s
    bad, _ = judge_latency({"generate_s": {"p95": 999.0}})
    assert not bad


# ── 拒答判定 ───────────────────────────────────────────────────────────── #


def test_is_refusal_and_verdict():
    assert is_refusal("抱歉，知识库中没有相关信息，无法回答该问题。")
    assert is_refusal("上下文中未提及该内容。")
    # 正常回答含「无法」但非拒答短语 → 不算拒答（marker 收束避免误报）
    assert not is_refusal("该专业无法转专业，需在大一末申请。")
    # 期望拒答：拒了得 1，硬答得 0
    assert refusal_verdict("抱歉，没有相关信息。", "refuse") == 1.0
    assert refusal_verdict("光速约为 299792458 米每秒。", "refuse") == 0.0
    # 期望作答：正常答得 1，误拒得 0
    assert refusal_verdict("中法学院用汉语、法语、英语教学。", "answer") == 1.0


def test_is_refusal_long_answer_not_counted():
    # 超长回答即使含 marker 也不是拒答（真拒答不会长篇展开）
    assert not is_refusal("无法忽略的是，该专业……" + "细节内容。" * 80)


# ── claim 级归因 ────────────────────────────────────────────────────────── #


def test_attribute_answer_splits_generation_vs_retrieval_errors():
    answer = "中法学院用三种语言教学。拿破仑出生于科西嘉岛。"
    contexts = ["中法学院 汉语 法语 英语 教学 语言"]
    # gold 未检回 → 无支持句归 retrieval_miss
    r1 = attribute_answer(answer, contexts, ["159"], ["100", "101"])
    assert r1["n_unsupported"] >= 1
    assert r1["retrieval_misses"] >= 1 and r1["generation_errors"] == 0
    # gold 已在检索结果 → 无支持句归 generation_error
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


# ── DomainRAG 子集 loader ──────────────────────────────────────────────── #


def test_domainrag_registry_covers_seven_subsets():
    subs = available_subsets()
    assert set(subs) == {"extractive", "noisy", "faithful", "multidoc",
                         "structured", "time_sensitive", "conversation"}
    assert len(SUBSET_FILES) == 7


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


def test_domainrag_subset_variants_normalize():
    corpus_recs = [
        {"id": 1, "title": "表格页", "url": "http://x/t", "contents": "分数线表"},
        {"id": 2, "title": "专业页", "url": "http://x/p", "contents": "专业介绍"},
    ]
    # structured：positive_reference 是无 id 的 dict → 按 title 反查 corpus id
    structured = [{"question": "甘肃分数线？", "answers": [["632"]],
                   "positive_reference": {"title": "表格页", "contents": "…"}}]
    _, gs = load_domainrag_from_records(corpus_recs, structured, subset="structured")
    assert gs[0].gold_doc_ids == ["1"]
    assert gs[0].category == "structured"

    # multidoc：positive_references（复数 list）
    md = [{"question": "两个专业对比？", "answer": "共同点与差异。",
           "positive_references": [{"id": 1}, {"title": "专业页"}]}]
    _, gs = load_domainrag_from_records(corpus_recs, md, subset="multidoc")
    assert sorted(gs[0].gold_doc_ids) == ["1", "2"]

    # faithful：answers + counterfact 元数据
    fa = [{"question": "截止时间？", "answers": [["12月20日"]],
           "counterfact_answers": [["12月19日"]], "commonsense": False,
           "positive_reference": {"id": 2, "title": "专业页"}}]
    _, gs = load_domainrag_from_records(corpus_recs, fa, subset="faithful")
    assert gs[0].gold_doc_ids == ["2"]
    assert gs[0].metadata["counterfact_answers"] == "12月19日"

    # conversation：history_qa + 指代追问 → turns
    conv = [{
        "history_qa": [{"question": "文学院前身是什么", "answers": ["华北联合大学文艺学院"],
                        "positive_reference": {"title": "表格页"}}],
        "question": "它成立于哪一年", "answers": ["1939年"],
        "positive_reference": {"title": "专业页"},
    }]
    _, gs = load_domainrag_from_records(corpus_recs, conv, subset="conversation")
    assert len(gs[0].turns) == 2
    assert gs[0].turns[0].gold_doc_ids == ["1"]
    assert gs[0].gold_doc_ids == ["2"]  # 末轮镜像
    assert gs[0].question == "它成立于哪一年"


def test_synth_loader():
    corpus, goldens = load_synth()
    assert len(corpus.docs) == 3
    assert len(goldens) == 2
    assert all(g.gold_doc_ids for g in goldens)


# ── KB 构建 / runner ───────────────────────────────────────────────────── #


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
    # 索引就绪
    assert kb.store.is_rag_ready()
    # 已知文档可被中文查询检索命中
    hits = kb.store.search("支付退款结算", top_k=3)
    assert hits, "检索应返回结果"
    assert any("支付" in h.text for h in hits)
    # doc id 可从 chunk 还原（检索轨的 doc 映射契约）
    id_map = doc_id_map(corpus)
    ids = retrieved_doc_ids(hits, id_map)
    assert any(i in ids for i in ("payment", "risk", "notify"))
    # 隔离：collection 名按 profile 隔离，data_dir 不在用户家目录
    assert kb.settings.vector_collection == "eval_test_build"
    assert kb.settings.data_dir != Path.home() / ".porto"


def test_build_eval_kb_milvus_backend_injection(tmp_path, monkeypatch):
    """vector_backend=milvus 时注入 Milvus repo（覆盖生产存储路径），默认 chroma。

    用「记录 uri + chroma 实现完整 repo 接口」的替身验证注入链路，不真连 Milvus。
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

    tc, _, _ = run_rag(golden, eval_kb, llm, strategy=QueryTransformStrategy.NONE, top_k=3)
    assert "未返回" in tc.actual_output  # guard 文案
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
    eval_kb.settings.condense_enabled = False  # 本测试只验历史进 prompt，不测改写
    chunk = SourceChunk(id="c1", path="eval_kb/payment.md", title="payment",
                        text="payment-service 负责支付、退款", score=0.9, metadata={})
    eval_kb.store.search.return_value = [chunk]
    llm = MagicMock()
    llm.complete.side_effect = ["是支付系统。", "负责支付、退款。"]

    mt = run_rag_multiturn(golden, eval_kb, llm, strategy=QueryTransformStrategy.NONE, top_k=3)

    assert len(mt.turns) == 2
    assert mt.timing["n_turns"] == 2
    # 第二轮 prompt 含第一轮历史（指代消解的上下文）
    second_user = llm.complete.call_args_list[1].kwargs["user"]
    assert "对话历史" in second_user and "支付服务是什么" in second_user
    # 末轮 test_case 组装
    assert mt.test_case.input == "它负责什么？"
    assert mt.test_case.actual_output == "负责支付、退款。"


def test_condense_gate_semantics():
    """门控节点：需要才改写；PASS/无历史/LLM 空均直通。"""
    from porto_chatbot.query_transform import condense_question, condense_with_gate

    # 需要改写：返回改写后的查询
    llm = MagicMock()
    llm.complete.return_value = "中国人民大学文学院成立于哪一年"
    r = condense_with_gate(
        llm, "它成立于哪一年",
        [("中国人民大学文学院的前身是什么", "华北联合大学文艺学院")],
    )
    assert r.needed is True and r.query == "中国人民大学文学院成立于哪一年"
    assert "对话历史" in llm.complete.call_args.args[1]

    # 已完整：LLM 输出 PASS → 直通不改写
    llm_pass = MagicMock()
    llm_pass.complete.return_value = "PASS"
    r2 = condense_with_gate(llm_pass, "中国人民大学文学院前身是什么", [("q1", "a1")])
    assert r2.needed is False and r2.query == "中国人民大学文学院前身是什么"

    # 无历史：零成本直通（不调 LLM）
    llm_none = MagicMock()
    r3 = condense_with_gate(llm_none, "任何问题", [])
    assert r3.needed is False and r3.reason == "no_history_or_no_llm"
    llm_none.complete.assert_not_called()

    # fail-open：LLM 返回空 → 原问题
    llm_empty = MagicMock()
    llm_empty.complete.return_value = ""
    assert condense_question(llm_empty, "Q", [("a", "b")]) == "Q"


def test_retrieve_with_transform_condense_node_wiring():
    """condense 是检索管线第一段：settings 开 + 有历史 → 改写后才检索；关 → 直通。"""
    from porto_chatbot.query_transform import retrieve_with_transform

    store = MagicMock()
    store.search.return_value = []
    settings = MagicMock()
    settings.rerank_enabled = False
    settings.condense_enabled = True
    llm = MagicMock()
    llm.complete.return_value = "支付服务负责什么"  # 门控改写

    retrieve_with_transform(
        "它呢", QueryTransformStrategy.NONE, store, settings, llm, 3,
        history=[("支付服务是什么", "支付系统。")],
    )
    # NONE 路径：检索查询是改写后的
    assert store.search.call_args.args[0] == "支付服务负责什么"

    # 开关关闭：不调 LLM、检索原问题
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
    """多轮 runner 走生产门控节点：第二轮检索查询被改写并记录。"""
    golden = RagGolden(
        question="它呢", reference_answer="A2", gold_doc_ids=["d2"],
        turns=[GoldenTurn("支付服务是什么", "支付系统。", ["d1"]),
               GoldenTurn("它呢", "A2", ["d2"])],
    )
    eval_kb = MagicMock()
    eval_kb.settings = MagicMock()
    eval_kb.settings.rerank_enabled = False
    eval_kb.settings.condense_enabled = True  # profile.condense_history 经 provision 注入
    eval_kb.store.search.return_value = []
    llm3 = MagicMock()
    # 调用序列：轮1 生成 →（轮2 有历史）门控改写 → 轮2 生成
    llm3.complete.side_effect = [
        "支付系统。",                 # 轮1 生成
        "支付服务负责什么",           # 轮2 门控改写（非 PASS）
        "负责支付。",                 # 轮2 生成
    ]
    mt = run_rag_multiturn(golden, eval_kb, llm3,
                           strategy=QueryTransformStrategy.NONE, top_k=3)
    # 第二轮检索用的是改写查询（生产节点内完成）
    assert eval_kb.store.search.call_args_list[-1].args[0] == "支付服务负责什么"
    assert mt.turns[1].condensed == "支付服务负责什么"
    assert mt.turns[0].condensed == ""  # 首轮无历史，门控零成本直通


def test_profiles_condense_variants():
    from tests.rag_eval.profiles import PROFILES, get_profile

    for name in ("condense", "condense_rerank", "semantic_embed"):
        assert name in PROFILES
    # 干净 A/B：condense 与 baseline 仅 condense_history 不同
    base = get_profile("baseline")
    cond = get_profile("condense")
    assert cond.condense_history is True and base.condense_history is False
    assert cond.max_chunk_chars == base.max_chunk_chars
    assert cond.rerank_enabled == base.rerank_enabled
    assert cond.strategy == base.strategy


# ── 指标聚合 / 判定 ─────────────────────────────────────────────────────── #


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
