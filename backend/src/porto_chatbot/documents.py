from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from docx import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from pypdf import PdfReader

from .logging_utils import get_component_logger
from .models.enums import DocumentParseMode, LocalParser

if TYPE_CHECKING:
    from .llm import LLMClient

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

class ContentFormat(StrEnum):
    MARKDOWN = "markdown"
    TEXT = "text"


class DocumentFormat(StrEnum):
    MARKDOWN = "markdown"
    TEXT = "text"
    PDF = "pdf"
    DOCX = "docx"


class ImageKind(StrEnum):
    EMBEDDED = "embedded"
    RELATIVE = "relative"
    REMOTE = "remote"
    DATA = "data"

_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")

logger = get_component_logger("documents")


class DocumentParseError(ValueError):
    """The uploaded document cannot be parsed safely."""


class DocumentLimitError(DocumentParseError):
    """The uploaded document exceeds an explicit resource limit."""


class DocumentNativeError(DocumentParseError):
    """Native model parsing was required but unavailable or failed."""


@dataclass
class DocumentImageRef:
    source: str
    alt: str = ""
    page: int | None = None
    kind: ImageKind = ImageKind.EMBEDDED


@dataclass
class DocumentArtifact:
    text: str
    format: DocumentFormat
    parser: str
    page_count: int | None = None
    image_refs: list[DocumentImageRef] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    used_native_vision: bool = False


@dataclass
class Chunk:
    """单个文档切分片段。

    text: chunk 正文（markdown 路径下会保留所属标题行作为语义上下文）
    metadata: 与该 chunk 相关的额外元信息，如 {"heading": "Title > Section A"}
    """

    text: str
    metadata: dict[str, str] = field(default_factory=dict)


def read_document(path: Path) -> str:
    """Backward-compatible text-only entry point used by KB indexing."""
    return parse_document(path).text


def parse_document(
    path: Path,
    *,
    original_name: str | None = None,
    max_bytes: int | None = None,
    max_pdf_pages: int | None = None,
    llm_client: LLMClient | None = None,
    mode: DocumentParseMode = DocumentParseMode.LOCAL,
    local_parser: LocalParser = LocalParser.PYPDF,
) -> DocumentArtifact:
    """Parse a document locally and optionally enrich a PDF with native model vision.

    Local parsing always happens first so ``hybrid`` can fall back deterministically.
    ``native`` is strict: unsupported capability, a failed request, or an empty model
    result raises :class:`DocumentNativeError`.
    """
    suffix = path.suffix.lower()
    logger.info("read document path=%s suffix=%s", path, suffix)
    if suffix not in SUPPORTED_EXTENSIONS:
        raise DocumentParseError(f"unsupported document type: {path.suffix}")
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise DocumentLimitError(
            f"document size exceeds limit: {path.stat().st_size} > {max_bytes} bytes"
        )

    try:
        artifact = _parse_local_document(
            path,
            suffix,
            max_pdf_pages=max_pdf_pages,
            local_parser=local_parser,
        )
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError(f"failed to parse document: {exc}") from exc

    if suffix != ".pdf" or mode == DocumentParseMode.LOCAL:
        return artifact

    filename = original_name or path.name
    supported = llm_client is not None and llm_client.document_capabilities.native_pdf
    if not supported:
        message = "configured model does not advertise native PDF understanding"
        if mode == DocumentParseMode.NATIVE:
            raise DocumentNativeError(message)
        artifact.warnings.append(message)
        return artifact

    try:
        enriched = llm_client.complete_document(
            filename,
            path.read_bytes(),
            "application/pdf",
            _NATIVE_PDF_PROMPT,
        )
        if not enriched or not enriched.strip():
            raise RuntimeError("model returned empty document text")
    except Exception as exc:
        if mode == DocumentParseMode.NATIVE:
            raise DocumentNativeError(f"native PDF parsing failed: {exc}") from exc
        artifact.warnings.append(f"native PDF parsing failed; used local text fallback: {exc}")
        return artifact

    artifact.text = enriched.strip()
    artifact.parser = f"{llm_client.settings.agent_provider}:native-pdf"
    artifact.used_native_vision = True
    return artifact


_NATIVE_PDF_PROMPT = """把这份 PRD 忠实转换为结构化 Markdown。保留标题、列表、表格、需求、约束和验收条件；读取并描述流程图、架构图、产品原型、截图和图表中与需求有关的信息。将视觉信息放在对应章节，注明来源页码。不要补写文档中不存在的需求，只输出 Markdown。"""


def _parse_local_document(
    path: Path,
    suffix: str,
    *,
    max_pdf_pages: int | None,
    local_parser: LocalParser,
) -> DocumentArtifact:
    if suffix in {".md", ".markdown", ".txt"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if suffix == ".txt":
            return DocumentArtifact(text=text, format=DocumentFormat.TEXT, parser="text")
        image_refs, warnings = _markdown_images(text)
        return DocumentArtifact(
            text=text,
            format=DocumentFormat.MARKDOWN,
            parser="markdown",
            image_refs=image_refs,
            warnings=warnings,
        )
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise DocumentParseError("encrypted PDF is not supported")
        page_count = len(reader.pages)
        if max_pdf_pages is not None and page_count > max_pdf_pages:
            raise DocumentLimitError(
                f"PDF pages exceed limit: {page_count} > {max_pdf_pages} pages"
            )
        texts: list[str] = []
        images: list[DocumentImageRef] = []
        for page_number, page in enumerate(reader.pages, start=1):
            texts.append(page.extract_text() or "")
            try:
                # Count image references without decoding their bytes. Decoding every
                # image here would waste memory and can amplify malicious PDFs.
                for image_number in range(1, len(page.images) + 1):
                    images.append(
                        DocumentImageRef(
                            source=f"page:{page_number}:image:{image_number}",
                            page=page_number,
                        )
                    )
            except Exception as exc:  # image enumeration must not discard usable text
                logger.warning("PDF image enumeration failed page=%s error=%s", page_number, exc)
        text = "\n".join(texts)
        parser = "pypdf"
        if local_parser == LocalParser.DOCLING:
            text = _parse_pdf_with_docling(path)
            parser = "docling"

        warnings = []
        if images:
            if parser == LocalParser.PYPDF:
                warnings.append(
                    f"PDF contains {len(images)} embedded image(s); local pypdf parsing cannot "
                    "understand their visual meaning"
                )
            else:
                warnings.append(
                    f"Docling processed {len(images)} embedded image(s); non-text diagram "
                    "semantics may still require native model vision"
                )
        return DocumentArtifact(
            text=text,
            format=DocumentFormat.PDF,
            parser=parser,
            page_count=page_count,
            image_refs=images,
            warnings=warnings,
        )
    if suffix == ".docx":
        doc = Document(str(path))
        images = [
            DocumentImageRef(source=f"inline-shape:{index}")
            for index, _ in enumerate(doc.inline_shapes, start=1)
        ]
        warnings = (
            ["DOCX embedded images are not interpreted by the local parser"] if images else []
        )
        return DocumentArtifact(
            text="\n".join(p.text for p in doc.paragraphs if p.text.strip()),
            format=DocumentFormat.DOCX,
            parser="python-docx",
            image_refs=images,
            warnings=warnings,
        )
    raise DocumentParseError(f"unsupported document type: {path.suffix}")


def _parse_pdf_with_docling(path: Path) -> str:
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise DocumentParseError(
            "Docling parser is not installed; run `uv sync --extra document-ai`"
        ) from exc
    try:
        result = DocumentConverter().convert(path)
        markdown = result.document.export_to_markdown()
    except Exception as exc:
        raise DocumentParseError(f"Docling failed to parse PDF: {exc}") from exc
    if not markdown or not markdown.strip():
        raise DocumentParseError("Docling returned no extractable document content")
    return markdown.strip()


def _markdown_images(text: str) -> tuple[list[DocumentImageRef], list[str]]:
    images: list[DocumentImageRef] = []
    has_relative = False
    has_remote = False
    for match in _MARKDOWN_IMAGE_RE.finditer(text):
        alt, source = match.groups()
        if source.startswith("data:image/"):
            kind = ImageKind.DATA
        elif source.startswith(("http://", "https://")):
            kind = ImageKind.REMOTE
            has_remote = True
        else:
            kind = ImageKind.RELATIVE
            has_relative = True
        images.append(DocumentImageRef(source=source, alt=alt, kind=kind))
    warnings: list[str] = []
    if has_relative:
        warnings.append("Markdown 包含相对图片引用；单文件上传无法读取关联资源")
    if has_remote:
        warnings.append("Markdown 包含远程图片引用；出于 SSRF 安全考虑未主动抓取")
    return images, warnings


def iter_documents(roots: list[Path]) -> list[tuple[Path, Path]]:
    """遍历多个根目录，返回 ``(root, file)`` 列表，按文件绝对路径去重。

    返回 root 是为了让上层生成 ``{root.name}/{相对 root 的路径}`` 显示标识。
    """
    seen: set[Path] = set()
    out: list[tuple[Path, Path]] = []
    for root in roots:
        if not root.exists():
            logger.info("iter documents root missing root=%s", root)
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() not in SUPPORTED_EXTENSIONS or p.name.startswith("~$"):
                continue
            real = p.resolve()
            if real in seen:
                continue
            seen.add(real)
            out.append((root, p))
    logger.info("iter documents roots=%s count=%s", [str(r) for r in roots], len(out))
    return out


def detect_content_format(path: Path) -> ContentFormat:
    """根据文件后缀推断切分策略：markdown 走标题切分，其余走纯文本递归切分。"""
    return ContentFormat.MARKDOWN if path.suffix.lower() in MARKDOWN_SUFFIXES else ContentFormat.TEXT


def chunk_text(text: str, *, max_chars: int, overlap: int) -> list[str]:
    """纯文本切分（向后兼容入口）；返回 chunk 正文列表，不带元信息。"""
    return [
        chunk.text
        for chunk in chunk_document(
            text, content_format=ContentFormat.TEXT, max_chars=max_chars, overlap=overlap
        )
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

    if content_format == ContentFormat.MARKDOWN:
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
        header_metadata[key].strip() for key in ("H1", "H2", "H3") if header_metadata.get(key)
    )
