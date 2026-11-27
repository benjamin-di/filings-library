"""Tests for the read-only Web Workspace JSON API, sandboxing, redline diff, and XBRL mapper."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
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


def test_suite_switcher_payload_and_auxiliary_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from signin_window.connections import SCRIPT_SOURCE, SuiteDirectory
    from signin_window.oidc import BrowserAuth, OIDCConfig

    svc, _, _, _ = _seed_service(tmp_path)
    directory = SuiteDirectory.from_json(
        json.dumps(
            {
                "name": "Connect",
                "endpoint": {
                    "title": "Assistant address",
                    "url": "https://mcp.example.com/mcp",
                    "help": "Copy into your assistant.",
                },
                "apps": [
                    {
                        "label": "Filings",
                        "url": "https://filings.example.com/",
                        "section": "Workspaces",
                        "description": "Read and compare company filings",
                        "groups": ["filings"],
                        "endpoint": True,
                    },
                    {
                        "label": "Household",
                        "url": "https://household.example.com/",
                        "section": "Workspaces",
                        "description": "Net worth, spending and review",
                        "groups": ["household"],
                    },
                    {
                        "label": "Admin",
                        "url": "https://admin.example.com/",
                        "section": "Tools",
                        "description": "Restricted admin console",
                        "groups": ["admins"],
                    },
                ],
            }
        )
    )

    state_dir = tmp_path / "browser"
    auth = BrowserAuth(
        OIDCConfig(
            issuer="https://idp.example.com",
            client_id="filings-web",
            public_url="https://filings.example.com",
            state_dir=state_dir,
            required_group="filings",
            cookie_name="__Host-filings",
            allow_loopback_http=True,
        ),
        suite=directory,
        home="/",
    )
    sid = "A" * 43
    auth.store.save(
        sid,
        {
            "issuer": "https://idp.example.com",
            "sub": "sub-alice",
            "name": "Alex Example",
            "label": "Alex Example",
            "groups": ["filings", "household"],
            "claims": {"person_id": "alex"},
            "expires": time.time() + 3600,
        },
    )

    app = create_http_app(
        svc,
        backend_bearer_token=BEARER,
        allowed_hosts=["testserver", "localhost"],
        browser_auth=auth,
        public_base_url="https://filings.example.com",
    )
    client = TestClient(app)

    # Unauthenticated /api/me stays fail-closed 401
    assert client.get("/api/me").status_code == 401

    client.cookies.set(auth.cookie, sid)
    me_resp = client.get("/api/me")
    assert me_resp.status_code == 200
    me = me_resp.json()
    assert me["has_logout"] is True
    suite = me["suite"]
    assert suite is not None
    assert suite["name"] == "Connect"
    assert suite["account"] == "Alex Example"
    assert "endpoint" not in suite
    labels = [link["label"] for link in suite["links"]]
    assert labels == ["Filings", "Household"]
    assert suite["links"][0]["current"] is True
    assert suite["links"][1]["current"] is False
    assert suite["links"][1]["section"] == "Workspaces"
    assert suite["links"][1]["url"] == "https://household.example.com/"

    # SPA shell has the switcher container hidden by default, accessible labels, and strict CSP
    spa = client.get("/")
    assert spa.status_code == 200
    assert 'id="suite-nav"' in spa.text
    assert 'aria-label="Your apps"' in spa.text
    assert '<summary aria-label="Apps">' in spa.text
    assert 'class="suite-mark h1"' in spa.text
    spa_csp = spa.headers["content-security-policy"]
    assert "script-src 'self';" in spa_csp
    assert "style-src 'self' 'unsafe-inline';" in spa_csp

    # Server-rendered 503 fallback uses signin_window.connections.page() with suite= and PAGE_HEADERS
    empty_static = tmp_path / "empty_static"
    empty_static.mkdir()
    app_missing = create_http_app(
        svc,
        backend_bearer_token=BEARER,
        allowed_hosts=["testserver", "localhost"],
        browser_auth=auth,
        public_base_url="https://filings.example.com",
        static_dir=empty_static,
    )
    client_missing = TestClient(app_missing)
    client_missing.cookies.set(auth.cookie, sid)
    fallback = client_missing.get("/")
    assert fallback.status_code == 503
    assert '<details class="suite-switch">' in fallback.text
    assert "https://household.example.com/" in fallback.text
    fallback_csp = fallback.headers["content-security-policy"]
    assert f"script-src {SCRIPT_SOURCE};" in fallback_csp
    assert "style-src 'self' 'unsafe-inline';" in fallback_csp

    # Unset or malformed SIGNIN_WINDOW_SUITE renders no switcher without error
    for env_val in (None, "{not-valid-json"):
        if env_val is None:
            monkeypatch.delenv("SIGNIN_WINDOW_SUITE", raising=False)
        else:
            monkeypatch.setenv("SIGNIN_WINDOW_SUITE", env_val)
        auth_no_suite = BrowserAuth(
            OIDCConfig(
                issuer="https://idp.example.com",
                client_id="filings-web",
                public_url="https://filings.example.com",
                state_dir=state_dir,
                required_group="filings",
                cookie_name="__Host-filings",
                allow_loopback_http=True,
            ),
        )
        app_no_suite = create_http_app(
            svc,
            backend_bearer_token=BEARER,
            allowed_hosts=["testserver", "localhost"],
            browser_auth=auth_no_suite,
            public_base_url="https://filings.example.com",
            static_dir=empty_static,
        )
        client_no_suite = TestClient(app_no_suite)
        client_no_suite.cookies.set(auth_no_suite.cookie, sid)
        me_no_suite = client_no_suite.get("/api/me")
        assert me_no_suite.status_code == 200
        assert me_no_suite.json()["suite"] is None
        fallback_no_suite = client_no_suite.get("/")
        assert fallback_no_suite.status_code == 503
        assert '<details class="suite-switch">' not in fallback_no_suite.text


def test_connections_home_redirects_and_oidc_callback_lands_on_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from signin_window.oidc import BrowserAuth, OIDCConfig

    svc, *_ = _seed_service(tmp_path)
    state_dir = tmp_path / "browser"
    auth = BrowserAuth(
        OIDCConfig(
            issuer="https://idp.example.com",
            client_id="filings-web",
            public_url="https://filings.example.com",
            state_dir=state_dir,
            required_group="filings",
            cookie_name="__Host-filings",
            allow_loopback_http=True,
        ),
        home="/",
    )
    assert auth.home == "/"

    app = create_http_app(
        svc,
        backend_bearer_token=BEARER,
        allowed_hosts=["testserver", "localhost"],
        browser_auth=auth,
        public_base_url="https://filings.example.com",
    )
    client = TestClient(app)

    # Unauthenticated /connections and /connections/ redirect to /connections/login with no-store
    for legacy_path in ("/connections", "/connections/"):
        unauth = client.get(legacy_path, follow_redirects=False)
        assert unauth.status_code == 303
        assert unauth.headers["location"] == "/connections/login"
        assert unauth.headers["cache-control"] == "no-store"

    # OIDC callback redirects directly to canonical / home (no extra /connections/ hop)
    pending_sid = "P" * 43
    auth.store.save(
        pending_sid,
        {
            "pending_expires": time.time() + 300,
        },
    )

    async def _fake_authorize_access_token(request: object, **kwargs: object) -> dict[str, object]:
        return {
            "id_token": "stub-id-token",
            "userinfo": {
                "iss": "https://idp.example.com",
                "sub": "user-a",
                "groups": ["filings"],
                "name": "Alex Example",
                "exp": time.time() + 3600,
            },
        }

    monkeypatch.setattr(auth.client, "authorize_access_token", _fake_authorize_access_token)
    client.cookies.set(auth.cookie, pending_sid)
    callback_resp = client.get("/connections/callback?code=abc&state=xyz", follow_redirects=False)
    assert callback_resp.status_code == 303
    assert callback_resp.headers["location"] == "/"

    # Resulting session can load canonical / home directly
    home_resp = client.get("/", follow_redirects=False)
    assert home_resp.status_code == 200

    # Authenticated /connections and /connections/ permanently redirect (308) to /
    for legacy_path in ("/connections", "/connections/"):
        authed = client.get(legacy_path, follow_redirects=False)
        assert authed.status_code == 308
        assert authed.headers["location"] == "/"
        assert authed.headers["cache-control"] == "no-store"


def test_cli_serve_configures_browser_auth_root_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    from filings_library import cli

    captured: dict[str, object] = {}

    orig_create = cli.create_http_app

    def _capture_create_http_app(*args: object, **kwargs: object) -> object:
        captured["browser_auth"] = kwargs.get("browser_auth")
        return orig_create(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "create_http_app", _capture_create_http_app)
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: None)

    rc = cli.main(
        [
            "serve",
            "--data-dir",
            str(tmp_path),
            "--targets-url",
            "http://127.0.0.1:9999",
            "--bearer-token",
            BEARER,
            "--public-url",
            "https://filings.example.com",
            "--oidc-issuer",
            "https://idp.example.com",
        ]
    )
    assert rc == 0
    browser_auth = captured["browser_auth"]
    assert browser_auth is not None
    assert getattr(browser_auth, "home") == "/"
