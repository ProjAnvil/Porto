from __future__ import annotations

from pathlib import Path

from tests.rag_eval.provision import build_eval_kb
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
