from __future__ import annotations

from pathlib import Path

import pytest

from porto_chatbot.documents import (
    chunk_document,
    chunk_text,
    detect_content_format,
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
