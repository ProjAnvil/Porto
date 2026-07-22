from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from porto_chatbot.documents import (
    DocumentLimitError,
    chunk_document,
    chunk_text,
    detect_content_format,
    iter_documents,
    parse_document,
)


@pytest.fixture()
def markdown_doc() -> str:
    return """# Payment Platform

payment-service handles payment authorization and refund.

## 风控模块

risk-service evaluates fraud rules before high value transactions.

### 规则引擎

rule-engine 在交易前同步执行风险规则集。

## 通知模块

notification-service sends payment result messages.
"""


def test_detect_content_format():
    assert detect_content_format(Path("a.md")) == "markdown"
    assert detect_content_format(Path("a.markdown")) == "markdown"
    assert detect_content_format(Path("a.txt")) == "text"
    assert detect_content_format(Path("a.pdf")) == "text"
    assert detect_content_format(Path("a.docx")) == "text"


def test_chunk_markdown_attaches_heading_path(markdown_doc: str):
    chunks = chunk_document(
        markdown_doc,
        content_format="markdown",
        max_chars=1400,
        overlap=0,
    )
    headings = {c.metadata.get("heading") for c in chunks}
    # 每个 section 都应带上标题路径
    assert "Payment Platform > 风控模块 > 规则引擎" in headings
    assert "Payment Platform > 通知模块" in headings


def test_chunk_markdown_preserves_header_in_text(markdown_doc: str):
    chunks = chunk_document(
        markdown_doc,
        content_format="markdown",
        max_chars=1400,
        overlap=0,
    )
    # strip_headers=False：标题行应保留在 chunk 正文里，作为语义上下文
    assert any("风控模块" in c.text for c in chunks)


def test_chunk_markdown_splits_oversized_section():
    body = "规则说明内容。\n" * 200  # 远超单 chunk 上限
    md = f"# Title\n\n## Section\n\n{body}"
    chunks = chunk_document(md, content_format="markdown", max_chars=300, overlap=20)
    assert len(chunks) > 1
    # 二次切分后每个 chunk 仍归属同一标题
    assert all(c.metadata.get("heading") == "Title > Section" for c in chunks)


def test_chunk_plain_has_no_heading():
    chunks = chunk_document(
        "纯文本文档，没有任何 markdown 标题。\n第二段内容。",
        content_format="text",
        max_chars=200,
        overlap=0,
    )
    assert chunks
    assert all("heading" not in c.metadata for c in chunks)


def test_chunk_text_wrapper_returns_strings_only():
    pieces = chunk_text("一段纯文本内容。", max_chars=200, overlap=0)
    assert pieces
    assert all(isinstance(p, str) for p in pieces)


def test_chunk_empty_returns_empty():
    assert chunk_document("   \n\n   ", content_format="markdown", max_chars=100, overlap=0) == []
    assert chunk_document("", content_format="text", max_chars=100, overlap=0) == []


def test_iter_documents_multi_root(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "x.md").write_text("# A", encoding="utf-8")
    (b / "x.md").write_text("# B", encoding="utf-8")
    (a / "ignore.log").write_text("nope", encoding="utf-8")

    result = iter_documents([a, b])
    roots = {r.name for r, _ in result}
    files = [p.name for _, p in result]
    assert roots == {"a", "b"}
    assert files.count("x.md") == 2  # 两个同名文件都保留
    assert "ignore.log" not in files  # 非支持扩展名过滤


def test_iter_documents_missing_root_skipped(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    (a / "x.md").write_text("# A", encoding="utf-8")
    result = iter_documents([a, tmp_path / "missing"])
    assert len(result) == 1 and result[0][1].name == "x.md"


def test_parse_markdown_reports_image_references(tmp_path):
    path = tmp_path / "prd.md"
    path.write_text(
        "# PRD\n\n![checkout flow](./assets/checkout.png)\n\n"
        "![remote](https://example.com/mock.png)",
        encoding="utf-8",
    )

    artifact = parse_document(path)

    assert artifact.text.startswith("# PRD")
    assert artifact.format == "markdown"
    assert [image.source for image in artifact.image_refs] == [
        "./assets/checkout.png",
        "https://example.com/mock.png",
    ]
    assert any("相对图片" in warning for warning in artifact.warnings)
    assert any("远程图片" in warning for warning in artifact.warnings)


def test_parse_pdf_reports_page_count_and_enforces_limit(tmp_path):
    path = tmp_path / "prd.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as stream:
        writer.write(stream)

    artifact = parse_document(path, max_pdf_pages=2)
    assert artifact.format == "pdf"
    assert artifact.page_count == 2

    with pytest.raises(DocumentLimitError, match="pages"):
        parse_document(path, max_pdf_pages=1)


def test_parse_document_enforces_file_size(tmp_path):
    path = tmp_path / "large.txt"
    path.write_text("12345", encoding="utf-8")
    with pytest.raises(DocumentLimitError, match="size"):
        parse_document(path, max_bytes=4)


def test_parse_pdf_uses_native_vision_and_hybrid_falls_back(tmp_path):
    path = tmp_path / "prd.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as stream:
        writer.write(stream)

    class FakeDocumentClient:
        document_capabilities = SimpleNamespace(native_pdf=True)
        settings = SimpleNamespace(agent_provider="openai")

        def complete_document(self, *args):
            return "# Vision PRD\n\n流程图包含 checkout-service。"

    artifact = parse_document(path, llm_client=FakeDocumentClient(), mode="hybrid")
    assert artifact.used_native_vision is True
    assert artifact.parser == "openai:native-pdf"
    assert "checkout-service" in artifact.text

    class FailingClient(FakeDocumentClient):
        def complete_document(self, *args):
            raise RuntimeError("unsupported file")

    fallback = parse_document(path, llm_client=FailingClient(), mode="hybrid")
    assert fallback.used_native_vision is False
    assert fallback.parser == "pypdf"
    assert any("fallback" in warning for warning in fallback.warnings)


def test_parse_pdf_with_optional_docling_backend(tmp_path, monkeypatch):
    path = tmp_path / "prd.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as stream:
        writer.write(stream)

    class FakeDoclingDocument:
        def export_to_markdown(self):
            return "# Docling PRD\n\n| A | B |\n| - | - |"

    class FakeConverter:
        def convert(self, source):
            assert source == path
            return SimpleNamespace(document=FakeDoclingDocument())

    fake_module = SimpleNamespace(DocumentConverter=FakeConverter)
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_module)

    artifact = parse_document(path, local_parser="docling")
    assert artifact.parser == "docling"
    assert artifact.text.startswith("# Docling PRD")
