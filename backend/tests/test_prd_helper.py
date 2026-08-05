"""Unit tests for ``agent.nodes._prd.read_prd_text``.

Covers the three coexisting paths exercised by workflow nodes:
- upload route: ``prd_file_id`` is a real FileService id → paginated read.
- text route / legacy: ``prd_file_id`` carries raw text → returned verbatim.
- no file_service / no prd_file_id → falls back to ``prd_text`` (or empty).
"""
from __future__ import annotations

from pathlib import Path

from porto_chatbot.agent.nodes._prd import read_prd_text
from porto_chatbot.files import FileService
from porto_chatbot.settings import Settings


def _file_service(tmp_path: Path) -> FileService:
    s = Settings(data_dir=tmp_path / "data")
    return FileService(s)


def test_read_prd_text_uses_file_service_when_file_id_matches(tmp_path):
    """Upload route: real file_id → file_service.read_pages(1, min(5, page_count))."""
    import io

    from fastapi import UploadFile

    svc = _file_service(tmp_path)
    # Markdown → virtual pages of 2000 chars each. 6012 chars → 4 virtual pages.
    body = "page-marker\n" + ("a" * 6000)
    meta = svc.store(
        UploadFile(filename="prd.md", file=io.BytesIO(body.encode())),
        owner_id="s1",
    )
    assert meta.page_count == 4  # sanity for the page math below

    out = read_prd_text({"prd_file_id": meta.file_id}, svc)
    # max_pages default 5 → reads all 4 pages (4 < 5)
    for n in range(1, meta.page_count + 1):
        assert f"第 {n} 页" in out
    assert f"第 {meta.page_count + 1} 页" not in out  # no page beyond page_count


def test_read_prd_text_caps_at_default_max_pages(tmp_path):
    """Long document: read_prd_text caps at 5 pages even when page_count > 5."""
    import io

    from fastapi import UploadFile

    svc = _file_service(tmp_path)
    body = "x" * 13000  # → 7 virtual pages of 2000 chars each
    meta = svc.store(
        UploadFile(filename="big.md", file=io.BytesIO(body.encode())),
        owner_id="s1",
    )

    out = read_prd_text({"prd_file_id": meta.file_id}, svc)
    for n in range(1, 6):
        assert f"第 {n} 页" in out
    assert "第 6 页" not in out  # capped


def test_read_prd_text_falls_back_when_file_id_not_in_db(tmp_path):
    """Text route: ``prd_file_id`` carries raw text (not a real id) → return verbatim."""
    svc = _file_service(tmp_path)
    raw = "# PRD\n目标：做一个支付系统。"
    out = read_prd_text({"prd_file_id": raw}, svc)
    assert out == raw


def test_read_prd_text_falls_back_to_prd_text_when_no_file_service():
    """No file_service injected (legacy / unit tests) → read prd_file_id, then prd_text."""
    assert read_prd_text({"prd_file_id": "raw-id-or-text"}) == "raw-id-or-text"
    assert read_prd_text({"prd_text": "legacy"}) == "legacy"
    assert read_prd_text({}) == ""


def test_read_prd_text_swallows_file_service_exceptions(tmp_path):
    """If file_service.get_info raises (corrupt sqlite / IO), fall back to raw string.

    Defensive: nodes must not crash on FileService errors — degrade to text path.
    """
    class _Broken:
        def get_info(self, file_id):
            raise RuntimeError("sqlite locked")

    out = read_prd_text({"prd_file_id": "raw-text"}, _Broken())
    assert out == "raw-text"
