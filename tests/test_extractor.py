"""Tests for HTML/iXBRL, XLSX, PDF extractors and stateless HTTP/ASGI service."""

from __future__ import annotations

import io

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


def test_pdf_extraction_rejects_encrypted_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed with EncryptedPdfError when a PDF requires a user password."""
    from filings_library.extractor import pdf_extract

    def _raise_encrypted(*_args: object, **_kwargs: object) -> None:
        raise EncryptedPdfError("PDF requires a user password")

    monkeypatch.setattr(pdf_extract, "_unlock_with_qpdf", _raise_encrypted)
    with pytest.raises(EncryptedPdfError):
        extract_pdf(b"%PDF-1.4\nencrypted-bytes")


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
