"""Exercise actual PDF decoding plus the scanned-page OCR handoff."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from api_gateway import document_intelligence as di


def write_pdf(path: Path, text: str = "Policy POL-2026-001 flood claim evidence") -> None:
    stream = f"BT /F1 12 Tf 40 100 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Root 1 0 R /Size 6 >>\nstartxref\n{xref}\n%%EOF".encode())
    path.write_bytes(data)


def test_pdf_text_citations_and_concurrent_requests(tmp_path):
    path = tmp_path / "native.pdf"
    write_pdf(path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: di._extract_pdf(path), range(8)))
    for lines, extractor in results:
        assert extractor == "pypdfium2"
        assert "POL-2026-001" in lines[0].text
        assert lines[0].page == 1 and lines[0].line_id == "p1-l1"
        assert lines[0].bbox is None
    path.unlink()  # All file handles must be released, including on Windows.


def test_sparse_pdf_renders_png_for_ocr(tmp_path, monkeypatch):
    path = tmp_path / "scanned.pdf"
    write_pdf(path, "")
    def ocr(image, page):
        assert image.startswith(b"\x89PNG\r\n\x1a\n")
        assert page == 1
        return [di.EvidenceLine("p1-ocr1", 1, "Scanned claim evidence", confidence=0.9)]
    monkeypatch.setattr(di, "_ocr_image", ocr)
    lines, extractor = di._extract_pdf(path)
    assert extractor == "pypdfium2+rapidocr" and lines[0].line_id == "p1-ocr1"


def test_page_limit_and_invalid_pdf_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "limited.pdf"
    write_pdf(path)
    monkeypatch.setattr(di, "MAX_PDF_PAGES", 0)
    with pytest.raises(ValueError, match="页数"):
        di._extract_pdf(path)
    path.write_bytes(b"not a pdf")
    with pytest.raises(Exception):
        di._extract_pdf(path)
