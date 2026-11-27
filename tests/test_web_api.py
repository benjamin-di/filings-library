"""Tests for the read-only Web Workspace JSON API, sandboxing, redline diff, and XBRL mapper."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.testclient import TestClient

from filings_library.cas import ContentAddressedStore
from filings_library.mcp_server import create_http_app
from filings_library.models import (
    CompanyTarget,
    DocumentRecord,
    ExtractedSection,
    FormFamily,
    SourceAdapter,
)
from filings_library.service import FilingsService, StaticScopeResolver
from filings_library.store import FilingsStore
from filings_library.web_api import (
    build_viewer_url,
    build_xbrl_statements,
    compute_section_diff,
    expand_sections_for_reader,
    format_display_form_type,
)

BEARER = "test-filings-bearer-token-32-chars-minimum-xyz"


def _seed_service(tmp_path: Path) -> tuple[FilingsService, str, str, str]:
    store = FilingsStore(tmp_path / "filings.sqlite3")
    cas = ContentAddressedStore(tmp_path / "cas")

    store.upsert_company(
        CompanyTarget(
            source_key="alpha", ticker="ALPH", exchange="NASDAQ", name="Alpha Corp", sec_cik=1001
        )
    )
    store.upsert_company(
        CompanyTarget(source_key="beta", ticker="BETA", exchange="HKEX", name="Beta Bio")
    )

    html_raw = b"<html><body><script>alert('xss')</script><p>Revenue grew 42%</p></body></html>"
    html_sha = cas.put_bytes(html_raw)
    pdf_raw = b"%PDF-1.4\n%fake-pdf\n"
    pdf_sha = cas.put_bytes(pdf_raw)

    doc_2026 = DocumentRecord(
        document_id="doc-alpha-2026",
        source_key="alpha",
        adapter=SourceAdapter.SEC_EDGAR,
        external_id="0001-26-000001",
        form_type="10-K",
        form_family=FormFamily.ANNUAL_REPORT,
        title="Alpha FY2026 10-K",
        filing_date="2026-02-20",
        period_end="2025-12-31",
        source_url="https://www.sec.gov/Archives/edgar/data/1001/a.htm",
        source_sha256=html_sha,
        source_mime="text/html",
        source_bytes=len(html_raw),
    )
    doc_2025 = DocumentRecord(
        document_id="doc-alpha-2025",
        source_key="alpha",
        adapter=SourceAdapter.SEC_EDGAR,
        external_id="0001-25-000001",
        form_type="10-K",
        form_family=FormFamily.ANNUAL_REPORT,
        title="Alpha FY2025 10-K",
        filing_date="2025-02-20",
        period_end="2024-12-31",
        source_url="https://www.sec.gov/Archives/edgar/data/1001/b.htm",
        source_sha256=html_sha,
        source_mime="text/html",
        source_bytes=len(html_raw),
    )
    doc_beta = DocumentRecord(
        document_id="doc-beta-2026",
        source_key="beta",
        adapter=SourceAdapter.HKEXNEWS,
        external_id="hkex-99",
        form_type="Financial Statements&#x2f;ESG Information - [Annual Report &#x2f; ESG]<br/>",
        form_family=FormFamily.ANNUAL_REPORT,
        title="Beta 2025 Annual Report",
        filing_date="2026-04-10",
        period_end="2025-12-31",
        source_url="https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0410/x.pdf",
        source_sha256=pdf_sha,
        source_mime="application/pdf",
        source_bytes=len(pdf_raw),
    )

    store.upsert_document(doc_2026)
    store.replace_sections(
        doc_2026.document_id,
        [
            ExtractedSection(
                locator="item:1a",
                title="Item 1A. Risk Factors",
                ordinal=0,
                markdown="Supply chain constraints eased in FY2026 as packaging capacity doubled.",
            ),
            ExtractedSection(
                locator="item:7",
                title="Item 7. MD&A",
                ordinal=1,
                markdown="Revenue reached $130,497 million driven by Blackwell demand.\n\n| Metric | FY2026 |\n|---|---|\n| Revenue | $130,497 |",
            ),
        ],
    )
    store.upsert_document(doc_2025)
    store.replace_sections(
        doc_2025.document_id,
        [
            ExtractedSection(
                locator="item:1a",
                title="Item 1A. Risk Factors",
                ordinal=0,
                markdown="Supply chain constraints remained tight in FY2025.",
            ),
        ],
    )
    store.upsert_document(doc_beta)
    store.replace_sections(
        doc_beta.document_id,
        [
            ExtractedSection(
                locator="page:1",
                title="Page 1",
                ordinal=0,
                markdown="Beta Bio clinical pipeline milestone report.",
            ),
        ],
    )

    facts_blob = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K",
                                "fy": 2026,
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                                "val": 130497000000,
                                "accn": "0001-26-000001",
                            }
                        ]
                    }
                }
            }
        }
    }
    facts_sha = cas.put_bytes(json.dumps(facts_blob).encode("utf-8"))
    store.upsert_company_facts("alpha", 1001, facts_sha)

    resolver = StaticScopeResolver({"sub-alice": ["alpha"], "sub-bob": ["beta"]})
    svc = FilingsService(store, cas, resolver)
    return svc, doc_2026.document_id, doc_2025.document_id, doc_beta.document_id


def _header_auth(request: Request) -> dict[str, Any] | None:
    sub = request.headers.get("x-test-user")
    if not sub:
        return None
    return {"sub": sub, "name": sub, "claims": {"person_id": sub}}


def test_web_api_scope_isolation_and_strict_raw_sandboxing(tmp_path: Path) -> None:
    svc, doc_2026, doc_2025, doc_beta = _seed_service(tmp_path)
    app = create_http_app(
        svc,
        backend_bearer_token=BEARER,
        allowed_hosts=["testserver", "localhost"],
        web_auth_resolver=_header_auth,
        public_base_url="https://filings.example.test",
    )
    client = TestClient(app)

    # Unauthenticated fails closed with 401
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/companies").status_code == 401
    assert client.get("/").status_code == 401

    alice = {"x-test-user": "sub-alice"}
    bob = {"x-test-user": "sub-bob"}

    # Alice sees only alpha, never beta
    me = client.get("/api/me", headers=alice)
    assert me.status_code == 200
    assert me.json()["company_count"] == 1
    assert me.headers["cache-control"] == "no-store"

    # Alice cannot read beta's document, diff, facts, or raw bytes
    assert client.get(f"/api/documents/{doc_beta}", headers=alice).status_code == 404
    assert client.get(f"/api/documents/{doc_beta}/raw", headers=alice).status_code == 404
    assert client.get("/api/companies/beta/facts", headers=alice).status_code == 404

    # Bob sees beta with cleaned HKEX display_form_type
    bob_docs = client.get("/api/documents?source_key=beta", headers=bob)
    assert bob_docs.status_code == 200
    assert bob_docs.json()["documents"][0]["display_form_type"] == "Annual Report"

    # Raw HTML response enforces strict CSP sandbox + nosniff
    raw_html = client.get(f"/api/documents/{doc_2026}/raw", headers=alice)
    assert raw_html.status_code == 200
    assert "sandbox" in raw_html.headers["content-security-policy"]
    assert "default-src 'none'" in raw_html.headers["content-security-policy"]
    assert raw_html.headers["x-content-type-options"] == "nosniff"
    assert raw_html.headers["cache-control"] == "no-store"

    # Raw PDF response enforces nosniff + inline PDF
    raw_pdf = client.get(f"/api/documents/{doc_beta}/raw", headers=bob)
    assert raw_pdf.status_code == 200
    assert raw_pdf.headers["content-type"] == "application/pdf"
    assert raw_pdf.headers["x-content-type-options"] == "nosniff"

    # Document detail returns prev_document_id and per-section hit counts
    detail = client.get(f"/api/documents/{doc_2026}?q=Blackwell", headers=alice)
    assert detail.status_code == 200
    body = detail.json()
    assert body["document"]["prev_document_id"] == doc_2025
    assert body["total_hits"] == 1
    assert body["outline"][1]["hit_count"] == 1

    # YoY word-level diff works across doc_2026 vs doc_2025
    diff = client.get(f"/api/documents/{doc_2026}/diff/{doc_2025}?locator=item:1a", headers=alice)
    assert diff.status_code == 200
    diff_json = diff.json()
    assert diff_json["added_words"] > 0
    assert diff_json["removed_words"] > 0

    # Standardized XBRL statement links back to doc_2026 (period derived from end date 2025-12-31)
    facts = client.get("/api/companies/alpha/facts", headers=alice)
    assert facts.status_code == 200
    fdata = facts.json()
    assert fdata["has_facts"] is True
    assert fdata["periods"] == ["FY2025"]
    assert fdata["rows"][0]["values"]["FY2025"]["document_id"] == doc_2026

    # Static SPA & assets serve with security headers and ETag/no-cache (never immutable unhashed)
    spa = client.get(f"/d/{doc_2026}?loc=item:7", headers=alice)
    assert spa.status_code == 200
    assert "Filings Library" in spa.text
    assert spa.headers["x-frame-options"] == "DENY"
    asset_resp = client.get("/assets/app.js")
    assert asset_resp.status_code == 200
    assert asset_resp.headers["cache-control"] == "no-cache"
    assert "etag" in asset_resp.headers
    assert (
        client.get(
            "/assets/app.js", headers={"If-None-Match": asset_resp.headers["etag"]}
        ).status_code
        == 304
    )
    assert client.get("/assets/../../pyproject.toml").status_code == 404

    # Strictly read-only: no POST upload route exists
    assert client.post("/api/documents/upload", headers=alice).status_code in (404, 405)


def test_helpers_hkex_cleanup_esef_chunking_diff_and_viewer_url() -> None:
    assert (
        format_display_form_type(
            "Financial Statements&#x2f;ESG Information - [Annual Report &#x2f; ESG]<br/>",
            "hkexnews",
        )
        == "Annual Report"
    )
    assert (
        build_viewer_url("https://filings.example.test/", "doc1", locator="item:7", query="margin")
        == "https://filings.example.test/d/doc1?loc=item%3A7&q=margin"
    )

    huge_md = "\n\n".join(
        f"## Heading {i}\n\n" + ("Financial disclosure paragraph text. " * 250) for i in range(12)
    )
    expanded = expand_sections_for_reader(
        [ExtractedSection(locator="section:document", title="", ordinal=0, markdown=huge_md)]
    )
    assert len(expanded) >= 3
    assert all(s.locator.startswith("section:document#p") for s in expanded)
    assert all(s.parent_locator == "section:document" for s in expanded)

    diff = compute_section_diff("Margin was 72.7% in FY2025.", "Margin was 75.0% in FY2026.")
    assert diff["added_words"] >= 2
    assert diff["removed_words"] >= 2

    stmts = build_xbrl_statements(
        {
            "facts": {
                "ifrs-full": {
                    "Revenue": {
                        "units": {
                            "EUR": [
                                {
                                    "form": "20-F",
                                    "fy": 2025,
                                    "end": "2025-12-31",
                                    "filed": "2026-02-15",
                                    "val": 28000000000,
                                    "accn": "0009-26-000001",
                                }
                            ]
                        }
                    }
                }
            }
        },
        doc_id_by_accn={"0009-26-000001": "doc-esef-1"},
    )
    assert stmts["periods"] == ["FY2025"]
    assert stmts["rows"][0]["values"]["FY2025"]["document_id"] == "doc-esef-1"


def test_xbrl_comparative_years_q4_filter_asc606_and_xlsx_filename() -> None:
    from filings_library.web_api import _sanitize_download_filename

    # 1. Single FY2025 10-K with 3 comparative years + 3-month Q4 entry all sharing fy=2025.
    facts_comparative = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2023-01-01",
                                "end": "2023-12-31",
                                "filed": "2026-02-20",
                                "val": 100_000_000_000,
                                "accn": "a-2025",
                            },
                            {
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "filed": "2026-02-20",
                                "val": 120_000_000_000,
                                "accn": "a-2025",
                            },
                            {
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                                "val": 150_000_000_000,
                                "accn": "a-2025",
                            },
                            {
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-10-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                                "val": 40_000_000_000,
                                "accn": "a-2025",
                            },
                        ]
                    }
                }
            }
        }
    }
    stmts = build_xbrl_statements(facts_comparative, doc_id_by_accn={"a-2025": "doc-2025"})
    assert stmts["periods"] == ["FY2025", "FY2024", "FY2023"]
    vals = stmts["rows"][0]["values"]
    assert vals["FY2025"]["val"] == 150_000_000_000
    assert vals["FY2024"]["val"] == 120_000_000_000
    assert vals["FY2023"]["val"] == 100_000_000_000

    # 2. ASC 606 concept switch merges legacy Revenues with newer contract revenue concept.
    facts_switch = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K",
                                "fy": 2017,
                                "fp": "FY",
                                "start": "2017-01-01",
                                "end": "2017-12-31",
                                "filed": "2018-02-20",
                                "val": 10_000_000_000,
                                "accn": "a-2017",
                            }
                        ]
                    }
                },
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                                "val": 150_000_000_000,
                                "accn": "a-2025",
                            }
                        ]
                    }
                },
            }
        }
    }
    stmts_sw = build_xbrl_statements(facts_switch, doc_id_by_accn={})
    assert stmts_sw["periods"] == ["FY2025", "FY2017"]
    assert stmts_sw["rows"][0]["concept"] == (
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    )
    assert stmts_sw["rows"][0]["values"]["FY2025"]["val"] == 150_000_000_000

    # 3. Download filename preserves .xlsx extension for spreadsheets.
    doc_xlsx = DocumentRecord(
        document_id="doc-xlsx-1",
        source_key="co1",
        adapter=SourceAdapter.EARNINGS_WATCH,
        external_id="e1",
        form_type="databook",
        form_family=FormFamily.DATABOOK,
        title="Databook",
        filing_date="2026-11-01",
        source_url="https://ir.example.test/d.xlsx",
        source_sha256="0" * 64,
        source_mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        source_bytes=100,
    )
    assert _sanitize_download_filename(doc_xlsx).endswith(".xlsx")

    # 4. Balance-sheet instant facts from the same 10-K prefer the latest end date (year-end over Q1).
    facts_bs = {
        "facts": {
            "us-gaap": {
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K",
                                "fy": 2025,
                                "end": "2025-03-31",
                                "filed": "2026-02-20",
                                "val": 12_000_000_000,
                                "accn": "a-2025",
                            },
                            {
                                "form": "10-K",
                                "fy": 2025,
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                                "val": 45_000_000_000,
                                "accn": "a-2025",
                            },
                        ]
                    }
                }
            }
        }
    }
    stmts_bs = build_xbrl_statements(facts_bs, doc_id_by_accn={})
    assert stmts_bs["rows"][0]["values"]["FY2025"]["end"] == "2025-12-31"
    assert stmts_bs["rows"][0]["values"]["FY2025"]["val"] == 45_000_000_000
