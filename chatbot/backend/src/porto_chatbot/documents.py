from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from .logging_utils import get_component_logger

SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf", ".docx"}
logger = get_component_logger("documents")


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


def chunk_text(text: str, *, max_chars: int, overlap: int) -> list[str]:
    normalized = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not normalized:
        logger.info("chunk text skipped empty input")
        return []
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=max_chars,
        chunk_overlap=overlap,
        separators=["\n## ", "\n### ", "\n\n", "\n", "。", "，", " ", ""],
        keep_separator=True,
    )
    chunks = [chunk.strip() for chunk in splitter.split_text(normalized) if chunk.strip()]
    logger.info(
        "chunk text chars=%s max_chars=%s overlap=%s chunks=%s",
        len(normalized),
        max_chars,
        overlap,
        len(chunks),
    )
    return chunks
