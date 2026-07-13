from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from docx import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from pypdf import PdfReader

from .logging_utils import get_component_logger

SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf", ".docx"}
MARKDOWN_SUFFIXES = {".md", ".markdown"}

# 标识当前切分策略版本。升级切分逻辑（如调整 header 层级、separators）时递增，
# vector_store 会把它写入 collection metadata，旧索引检测到不一致即自动 rebuild。
SPLITTER_VERSION = "markdown-header-v1"

# markdown header split 识别的标题层级：(标记, 元数据 key)
_MARKDOWN_HEADERS: list[tuple[str, str]] = [
    ("#", "H1"),
    ("##", "H2"),
    ("###", "H3"),
]

# 二次切分（把过大的 section / 纯文本文档切成最终 chunk）使用的递归分隔符。
_PLAIN_SEPARATORS = ["\n\n", "\n", "。", "，", " ", ""]

ContentFormat = Literal["markdown", "text"]

logger = get_component_logger("documents")


@dataclass
class Chunk:
    """单个文档切分片段。

    text: chunk 正文（markdown 路径下会保留所属标题行作为语义上下文）
    metadata: 与该 chunk 相关的额外元信息，如 {"heading": "Title > Section A"}
    """

    text: str
    metadata: dict[str, str] = field(default_factory=dict)


def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    logger.info("read document path=%s suffix=%s", path, suffix)
    if suffix in {".md", ".markdown", ".txt"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    raise ValueError(f"Unsupported document type: {path.suffix}")


def iter_documents(root: Path) -> list[Path]:
    if not root.exists():
        logger.info("iter documents root missing root=%s", root)
        return []
    documents = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS and not p.name.startswith("~$")
    )
    logger.info("iter documents root=%s count=%s", root, len(documents))
    return documents


def detect_content_format(path: Path) -> ContentFormat:
    """根据文件后缀推断切分策略：markdown 走标题切分，其余走纯文本递归切分。"""
    return "markdown" if path.suffix.lower() in MARKDOWN_SUFFIXES else "text"


def chunk_text(text: str, *, max_chars: int, overlap: int) -> list[str]:
    """纯文本切分（向后兼容入口）；返回 chunk 正文列表，不带元信息。"""
    return [
        chunk.text
        for chunk in chunk_document(text, content_format="text", max_chars=max_chars, overlap=overlap)
    ]


def chunk_document(
    text: str,
    *,
    content_format: ContentFormat,
    max_chars: int,
    overlap: int,
) -> list[Chunk]:
    """按文档格式切分，返回带元信息的 chunk 列表。

    markdown 文档先按标题（#/##/###）切块，标题路径写入 chunk.metadata["heading"]；
    每个 section 再用 RecursiveCharacterTextSplitter 做长度二次切分，避免单 section 过大。
    纯文本文档（txt/pdf/docx）直接走递归切分。
    """
    normalized = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not normalized:
        logger.info("chunk document skipped empty input format=%s", content_format)
        return []

    if content_format == "markdown":
        chunks = _split_markdown(normalized, max_chars=max_chars, overlap=overlap)
    else:
        chunks = _split_plain(normalized, max_chars=max_chars, overlap=overlap)

    logger.info(
        "chunk document chars=%s format=%s max_chars=%s overlap=%s chunks=%s",
        len(normalized),
        content_format,
        max_chars,
        overlap,
        len(chunks),
    )
    return chunks


def _make_chunker(max_chars: int, overlap: int) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=max_chars,
        chunk_overlap=overlap,
        separators=_PLAIN_SEPARATORS,
        keep_separator=True,
    )


def _split_plain(text: str, *, max_chars: int, overlap: int) -> list[Chunk]:
    chunker = _make_chunker(max_chars, overlap)
    return [Chunk(text=c.strip()) for c in chunker.split_text(text) if c.strip()]


def _split_markdown(text: str, *, max_chars: int, overlap: int) -> list[Chunk]:
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=_MARKDOWN_HEADERS,
        strip_headers=False,  # 保留标题行作为 chunk 语义上下文
    )
    sections = header_splitter.split_text(text)
    chunker = _make_chunker(max_chars, overlap)

    chunks: list[Chunk] = []
    for section in sections:
        heading = _join_heading(section.metadata)
        metadata = {"heading": heading} if heading else {}
        for piece in chunker.split_text(section.page_content):
            piece = piece.strip()
            if piece:
                chunks.append(Chunk(text=piece, metadata=metadata))
    return chunks


def _join_heading(header_metadata: dict[str, str]) -> str:
    """把 {'H1': 'Title', 'H2': 'Section A'} 拼成 'Title > Section A'。"""
    return " > ".join(
        header_metadata[key].strip()
        for key in ("H1", "H2", "H3")
        if header_metadata.get(key)
    )
