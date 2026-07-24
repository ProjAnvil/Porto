"""Generate a minimal valid PDF for spike / test fixtures (no external deps).

手工构造 PDF 1.4 单页文本(英文,Helvetica),供 complete_document spike 与
document 解析测试使用。pypdf 可读。

    cd backend && python -m scripts.make_test_pdf
"""
from __future__ import annotations

from pathlib import Path


def make_pdf(path: Path, lines: list[str]) -> None:
    # content stream:每行一个 BT...ET 文本块
    y = 700
    ops: list[str] = []
    for line in lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        ops.append(f"BT /F1 18 Tf 72 {y} Td ({escaped}) Tj ET")
        y -= 28
    content = "\n".join(ops).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        content,  # obj 4:content stream
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        if i == 4:
            body = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(obj), obj)
        else:
            body = obj
        pdf += b"%d 0 obj\n%s\nendobj\n" % (i, body)

    xref_pos = len(pdf)
    n = len(objects) + 1
    pdf += b"xref\n0 %d\n" % n
    pdf += b"0000000000 65535 f \r\n"
    for off in offsets:
        pdf += b"%010d 00000 n \r\n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % n
    pdf += b"startxref\n%d\n%%%%EOF" % xref_pos
    path.write_bytes(pdf)


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "test_prd.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    make_pdf(
        out,
        [
            "PRD Test Document",
            "Requirement 1: User can create an order.",
            "Requirement 2: Payment supports refund and settlement.",
            "Requirement 3: Notification on order success.",
            "Requirement 4: Risk control for fraud detection.",
        ],
    )
    print(f"wrote {out} ({out.stat().st_size} bytes)")
