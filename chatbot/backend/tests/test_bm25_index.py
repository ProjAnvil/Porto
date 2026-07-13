from __future__ import annotations

from pathlib import Path

from porto_chatbot.bm25_index import Bm25Index, Bm25Registry, ChunkRecord


def _sample_chunks() -> list[ChunkRecord]:
    return [
        ChunkRecord("c1", "支付服务处理支付授权、退款、结算与渠道路由。", {"path": "a/pay.md", "title": "pay"}),
        ChunkRecord("c2", "风控服务在高价值交易前评估欺诈规则。", {"path": "a/risk.md", "title": "risk"}),
        ChunkRecord("c3", "通知服务发送支付结果消息给商户和用户。", {"path": "a/notify.md", "title": "notify"}),
    ]


def test_bm25_build_returns_relevant_ids_sorted():
    idx = Bm25Index()
    idx.build(_sample_chunks())
    hits = idx.query("退款 结算", top_k=3)
    assert hits, "expected non-empty results"
    ids = [h[0] for h in hits]
    assert "c1" in ids
    # 支付/退款/结算相关文档应排在风控/通知之前
    assert ids[0] == "c1"


def test_bm25_save_load_roundtrip(tmp_path: Path):
    idx = Bm25Index()
    idx.build(_sample_chunks())
    idx.save(tmp_path / "bm25")

    loaded = Bm25Index.load(tmp_path / "bm25")
    assert len(loaded) == 3
    hits_before = idx.query("风控 欺诈", top_k=2)
    hits_after = loaded.query("风控 欺诈", top_k=2)
    assert [h[0] for h in hits_before] == [h[0] for h in hits_after]
    assert hits_after[0][0] == "c2"  # 风控文档最相关


def test_registry_build_get_invalidate(sample_settings, tmp_path: Path):
    # sample_settings.data_dir 是 tmp 下的独立目录
    chunks = _sample_chunks()
    built = Bm25Registry.build_and_save(sample_settings, chunks)
    assert len(built) == 3
    assert (sample_settings.data_dir / "bm25s_index").exists()

    # get 命中缓存
    got = Bm25Registry.get(sample_settings)
    assert got is not None
    assert [h[0] for h in got.query("通知 消息", top_k=1)] == ["c3"]

    # invalidate 后重新从磁盘加载
    Bm25Registry.invalidate(sample_settings.data_dir)
    assert str(sample_settings.data_dir) not in Bm25Registry._cache
    reloaded = Bm25Registry.get(sample_settings)
    assert reloaded is not None and len(reloaded) == 3


def test_registry_get_returns_none_when_absent(sample_settings):
    Bm25Registry.invalidate(sample_settings.data_dir)
    assert Bm25Registry.get(sample_settings) is None
