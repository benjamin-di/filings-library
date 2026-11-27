"""Tests for HTML/iXBRL, XLSX, PDF extractors and stateless HTTP/ASGI service."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import openpyxl
import pytest

from filings_library.extractor import (
    EncryptedPdfError,
    ExtractorClient,
    extract_html_ixbrl,
    extract_pdf,
    extract_xlsx,
)
from filings_library.extractor.html_ixbrl import UNTRUSTED_HEADER


def _build_minimal_pdf(pages: list[str]) -> bytes:
    """Build a deterministic, valid multi-page PDF 1.4 with native Helvetica text."""
    objects: list[bytes] = []

    # 1: Catalog, 2: Pages, 3: Font
    num_pages = len(pages)
    page_ids = [4 + i * 2 for i in range(num_pages)]
    kids_str = " ".join(f"{pid} 0 R" for pid in page_ids)

    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objects.append(
        f"2 0 obj\n<< /Type /Pages /Kids [{kids_str}] /Count {num_pages} >>\nendobj\n".encode()
    )
    objects.append(b"3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")

    for idx, text in enumerate(pages):
        page_id = 4 + idx * 2
        content_id = page_id + 1
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n".encode("latin-1")
        objects.append(
            (
                f"{page_id} 0 obj\n"
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>\n"
                f"endobj\n"
            ).encode()
        )
        objects.append(
            f"{content_id} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"endstream\nendobj\n"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)

    xref_offset = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode())

    out.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(out)


def _build_sample_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws1 = wb.active
    assert ws1 is not None
    ws1.title = "Income Statement"
    ws1.append(["Metric", "FY2025", "FY2024"])
    ws1.append(["Revenue", 22594, 19800])
    ws1.append(["Net Income", 4120, 3510])

    ws2 = wb.create_sheet("Segment Mix")
    ws2.append(["Segment", "Share"])
    ws2.append(["Cloud", "62%"])

    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def test_html_ixbrl_colspan_and_currency_paren_merging() -> None:
    html = """
    <html>
      <head>
        <style>.hidden { display: none; }</style>
        <script>console.log("ignore me");</script>
      </head>
      <body>
        <ix:header><ix:hidden>SECRET_XBRL_CONTEXT_NOISE</ix:hidden></ix:header>
        <h2>Item 7. Management's Discussion and Analysis</h2>
        <p>Consolidated revenue increased year over year.</p>
        <table>
          <tr>
            <th>Metric</th>
            <th colspan="3">FY2025</th>
            <th colspan="3">FY2024</th>
          </tr>
          <tr>
            <td>Revenue</td>
            <td>$</td><td>22,594</td><td></td>
            <td>$</td><td>19,800</td><td></td>
          </tr>
          <tr>
            <td>Operating Loss</td>
            <td>$</td><td>(1,250</td><td>)</td>
            <td>$</td><td>(980</td><td>)</td>
          </tr>
        </table>
      </body>
    </html>
    """
    extracted = extract_html_ixbrl(html.encode("utf-8"))
    assert extracted.locator_kind == "section"
    assert extracted.markdown.startswith(UNTRUSTED_HEADER)
    assert "SECRET_XBRL_CONTEXT_NOISE" not in extracted.markdown
    assert "console.log" not in extracted.markdown
    assert len(extracted.sections) == 1
    sec = extracted.sections[0]
    assert sec.locator == "item:7"

    # Verify colspan alignment and currency/paren cell merging.
    assert "| Metric | FY2025 | FY2024 |" in sec.markdown
    assert "| Revenue | $22,594 | $19,800 |" in sec.markdown
    assert "| Operating Loss | $(1,250) | $(980) |" in sec.markdown


def test_xlsx_extraction() -> None:
    xlsx_bytes = _build_sample_xlsx()
    extracted = extract_xlsx(xlsx_bytes)
    assert extracted.locator_kind == "section"
    locators = [s.locator for s in extracted.sections]
    assert locators == [
        "section:sheet-index",
        "section:sheet-income-statement",
        "section:sheet-segment-mix",
    ]
    assert "| Revenue | 22594 | 19800 |" in extracted.sections[1].markdown
    assert "| Cloud | 62% |" in extracted.sections[2].markdown


def test_pdf_extraction_native_pages_and_anchors() -> None:
    pdf_bytes = _build_minimal_pdf(
        [
            "Annual Report 2025 Consolidated Revenue 22,594 million",
            "Page Two Segment Operating Margin Discussion and Analysis",
        ]
    )
    extracted = extract_pdf(pdf_bytes)
    assert extracted.locator_kind == "page"
    assert extracted.page_count == 2
    assert [s.locator for s in extracted.sections] == ["page:1", "page:2"]
    assert "<!-- page: 1 -->" in extracted.markdown
    assert "<!-- page: 2 -->" in extracted.markdown
    assert "22,594" in extracted.sections[0].markdown
    assert "Operating Margin" in extracted.sections[1].markdown


def test_pdf_extraction_rejects_encrypted_pdf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail closed with EncryptedPdfError when qpdf/pdftotext report password errors."""
    import subprocess

    from filings_library.extractor import pdf_extract

    # Directly verify _is_password_error against real qpdf/poppler error strings.
    assert pdf_extract._is_password_error("qpdf: input.pdf: invalid password")
    assert pdf_extract._is_password_error("Command Line Error: Incorrect password")
    assert pdf_extract._is_password_error("PDF requires a password")
    assert not pdf_extract._is_password_error("syntax warning: repaired xref")

    def _fake_qpdf_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert "--decrypt" in cmd
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=2,
            stdout="",
            stderr="qpdf: input.pdf: invalid password\n",
        )

    monkeypatch.setattr(pdf_extract.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(pdf_extract.subprocess, "run", _fake_qpdf_run)

    src = tmp_path / "enc.pdf"
    dst = tmp_path / "dec.pdf"
    src.write_bytes(b"%PDF-1.4\nencrypted-bytes")
    warnings: list[str] = []
    with pytest.raises(EncryptedPdfError, match="invalid password"):
        pdf_extract._unlock_with_qpdf(src, dst, warnings)

    with pytest.raises(EncryptedPdfError, match="invalid password"):
        extract_pdf(b"%PDF-1.4\nencrypted-bytes")


def test_html_ixbrl_wrapper_blocks_nested_table_and_empty_rows() -> None:
    html = """
    <html>
      <body>
        <ix:nonNumeric name="us-gaap:DisclosureTextBlock">
          Introductory note before heading.
          <h2>Item 8. Financial Statements</h2>
          <p>Lead-in text</p>
          <table>
            <tr></tr>
            <tr>
              <th rowspan="2">Line Item</th>
              <th>FY2025</th>
            </tr>
            <tr></tr>
            <tr>
              <td>2025 Actual</td>
            </tr>
            <tr>
              <td>
                Revenue
                <table>
                  <tr><td>Nested Footnote A</td><td>123</td></tr>
                </table>
              </td>
              <td>$22,594</td>
            </tr>
          </table>
        </ix:nonNumeric>
      </body>
    </html>
    """
    extracted = extract_html_ixbrl(html.encode("utf-8"))
    # Text before heading is preserved as its own section; heading inside ix:nonNumeric is detected.
    assert "Introductory note before heading." in extracted.markdown
    locators = [s.locator for s in extracted.sections]
    assert "item:8" in locators
    item8 = next(s for s in extracted.sections if s.locator == "item:8")
    assert "Lead-in text" in item8.markdown
    # Outer table must NOT pull nested <table> <tr> rows as outer rows, and empty <tr> must not
    # skew rowspan alignment: "2025 Actual" belongs in column 2 (under FY2025), not column 1!
    table_lines = [line for line in item8.markdown.splitlines() if line.startswith("|")]
    assert table_lines[0] == "| Line Item | FY2025 |"
    assert table_lines[2] == "|  | 2025 Actual |"
    assert len(table_lines) == 4  # header, sep, rowspan-2nd-row, Revenue row (no nested rows)
    assert "$22,594" in table_lines[3]


def test_pdf_subprocess_timeouts_ocr_cap_and_png_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess
    from types import SimpleNamespace

    from filings_library.extractor import pdf_extract

    # 1. qpdf + pdftotext TimeoutExpired are caught and recorded as warnings.
    def _timeout_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    monkeypatch.setattr(pdf_extract.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(pdf_extract.subprocess, "run", _timeout_run)

    src = tmp_path / "slow.pdf"
    dst = tmp_path / "out.pdf"
    src.write_bytes(b"%PDF-1.4\n")
    warnings: list[str] = []
    assert pdf_extract._unlock_with_qpdf(src, dst, warnings, timeout=1) == src
    assert any("qpdf timed out" in w for w in warnings)

    assert pdf_extract._extract_native_pages_pdftotext(src, warnings, timeout=1) is None
    assert any("pdftotext timed out" in w for w in warnings)

    # 2. _ocr_page unlinks page_N.png in finally even when tesseract times out.
    class _FakeBitmap:
        def to_pil(self) -> Any:
            return SimpleNamespace(save=lambda path: Path(path).write_bytes(b"PNG"))

    class _FakePage:
        def render(self, scale: float) -> _FakeBitmap:
            del scale
            return _FakeBitmap()

        def close(self) -> None:
            pass

    class _FakeDoc:
        def __init__(self, _path: str) -> None:
            pass

        def __getitem__(self, _idx: int) -> _FakePage:
            return _FakePage()

        def close(self) -> None:
            pass

    import sys

    monkeypatch.setitem(sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=_FakeDoc))
    ocr_warnings: list[str] = []
    assert pdf_extract._ocr_page(src, 0, tmp_path, ocr_warnings, timeout=1) is None
    assert any("tesseract timed out" in w for w in ocr_warnings)
    assert not (tmp_path / "page_1.png").exists()

    # 3. max_ocr_pages caps OCR invocations and records a warning.
    monkeypatch.setattr(pdf_extract, "_unlock_with_qpdf", lambda s, d, w: s)
    monkeypatch.setattr(
        pdf_extract, "_extract_native_pages_pdftotext", lambda p, w=None: ["", "", ""]
    )
    ocr_calls: list[int] = []

    def _fake_ocr(p: Path, idx: int, td: Path, w: list[str]) -> str | None:
        del p, td, w
        ocr_calls.append(idx)
        return f"OCR text page {idx + 1} with sufficient characters"

    monkeypatch.setattr(pdf_extract, "_ocr_page", _fake_ocr)
    doc = extract_pdf(b"%PDF-1.4\n", max_ocr_pages=2)
    assert ocr_calls == [0, 1]
    assert doc.ocr_pages == [1, 2]
    assert any("OCR page cap (2) exceeded" in w for w in doc.warnings)


@pytest.mark.asyncio
async def test_extractor_streaming_413_rejects_oversized_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from filings_library.extractor import app as extractor_app

    monkeypatch.setattr(extractor_app, "MAX_EXTRACT_BYTES", 64)
    asgi_app = extractor_app.create_extractor_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="http://extractor.local",
    ) as client:
        # 1. Early rejection via Content-Length header
        resp_cl = await client.post(
            "/extract",
            params={"mime_type": "text/html", "filename": "big.html"},
            content=b"x" * 100,
        )
        assert resp_cl.status_code == 413
        assert resp_cl.json()["error"] == "payload_too_large"

        # 2. Streaming rejection without Content-Length (chunked generator)
        async def _chunks() -> Any:
            yield b"<html><body>" + b"a" * 40
            yield b"b" * 40 + b"</body></html>"

        resp_stream = await client.post(
            "/extract",
            params={"mime_type": "text/html", "filename": "stream.html"},
            content=_chunks(),
        )
        assert resp_stream.status_code == 413
        assert resp_stream.json()["error"] == "payload_too_large"


@pytest.mark.asyncio
async def test_stateless_extractor_http_asgi_service() -> None:
    client = ExtractorClient.in_process()
    health = await client.health()
    assert health["status"] == "ok"
    assert "capabilities" in health

    # HTML extraction over ASGI
    html_res = await client.extract(
        b"<html><body><h2>Revenue Overview</h2><p>Revenue was $22,594M.</p></body></html>",
        mime_type="text/html",
        filename="filing.htm",
    )
    assert html_res.sections[0].locator == "section:revenue-overview"
    assert "$22,594M" in html_res.markdown

    # XLSX extraction over ASGI
    xlsx_res = await client.extract(
        _build_sample_xlsx(),
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="databook.xlsx",
    )
    assert xlsx_res.sections[0].locator == "section:sheet-index"

    # PDF extraction over ASGI
    pdf_res = await client.extract(
        _build_minimal_pdf(["Synthetic Filing Page One Native Text Layer"]),
        mime_type="application/pdf",
        filename="report.pdf",
    )
    assert pdf_res.page_count == 1
    assert pdf_res.sections[0].locator == "page:1"


def test_unique_locators_for_xlsx_index_sheet_and_colliding_html_headings() -> None:
    wb = openpyxl.Workbook()
    ws1 = wb.active
    assert ws1 is not None
    ws1.title = "Index"
    ws1.append(["Tab", "Purpose"])
    ws1.append(["Summary", "Key Metrics"])
    ws2 = wb.create_sheet("Summary")
    ws2.append(["Metric", "Val"])
    ws3 = wb.create_sheet("Summary 2")
    ws3.append(["Metric", "Val"])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()

    ext_xlsx = extract_xlsx(buf.getvalue())
    xlsx_locs = [s.locator for s in ext_xlsx.sections]
    assert len(xlsx_locs) == len(set(xlsx_locs))
    assert xlsx_locs[0] == "section:sheet-index"
    assert xlsx_locs[1] == "section:sheet-index-2"

    html = (
        "<html><body>"
        "<h2>Overview</h2><p>A</p>"
        "<h2>Overview</h2><p>B</p>"
        "<h2>Overview 2</h2><p>C</p>"
        "</body></html>"
    )
    ext_html = extract_html_ixbrl(html)
    html_locs = [s.locator for s in ext_html.sections]
    assert html_locs == ["section:overview", "section:overview-2", "section:overview-2-2"]
