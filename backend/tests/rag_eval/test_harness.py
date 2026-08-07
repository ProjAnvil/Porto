from __future__ import annotations

from tests.rag_eval.schema import CorpusDoc, RagCorpus, RagGolden


def test_schema_constructs():
    corpus = RagCorpus(docs=[CorpusDoc(id="d1", text="你好", metadata={"k": "v"})])
    g = RagGolden(question="Q?", reference_answer="A.", gold_doc_ids=["d1"])
    assert corpus.docs[0].id == "d1"
    assert corpus.docs[0].text == "你好"
    assert g.category == "default"
    assert g.gold_doc_ids == ["d1"]
