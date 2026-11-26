"""Tests for SEC EDGAR, HKEXnews, ESEF XBRL, Earnings Watch, TargetsClient, and sync_once."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from filings_library.adapters.earnings_watch import EarningsWatchAdapter
from filings_library.adapters.esef_xbrl import EsefXbrlAdapter
from filings_library.adapters.hkexnews import HkexnewsAdapter, is_kept_hkex_filing
from filings_library.adapters.sec_edgar import SecEdgarAdapter, query_facts
from filings_library.cas import ContentAddressedStore
from filings_library.extractor.app import ExtractorClient
from filings_library.models import CompanyTarget, FormFamily, VerifiedDocumentHint
from filings_library.search import search_filings
from filings_library.store import FilingsStore
from filings_library.targets_client import HubUnavailableError, LibraryTargetsClient
from filings_library.worker import SyncConfig, export_markdown, sync_once

SEC_UA = "FilingsLibraryTest/0.1 (contact@example.test)"


def test_sec_user_agent_enforcement() -> None:
    with pytest.raises(ValueError):
        SecEdgarAdapter(user_agent="")
    with pytest.raises(ValueError):
        SecEdgarAdapter(user_agent="NoEmailAgent/1.0")
    adapter = SecEdgarAdapter(user_agent=SEC_UA)
    assert adapter.user_agent == SEC_UA


@pytest.mark.asyncio
async def test_sec_edgar_10q_6k_ex99_and_companyfacts() -> None:
    submissions_payload = {
        "cik": "0000123456",
        "filings": {
            "recent": {
                "form": ["10-Q", "6-K", "8-K", "8-K", "10-K"],
                "accessionNumber": [
                    "0000123456-26-000010",
                    "0000123456-26-000020",
                    "0000123456-26-000030",
                    "0000123456-26-000040",
                    "0000123456-20-000001",  # outside 3-year backfill
                ],
                "filingDate": [
                    "2026-08-05",
                    "2026-09-10",
                    "2026-08-01",
                    "2026-08-02",
                    "2020-03-01",
                ],
                "reportDate": [
                    "2026-06-30",
                    "2026-09-10",
                    "2026-08-01",
                    "2026-08-02",
                    "2019-12-31",
                ],
                "primaryDocument": [
                    "q2-2026.htm",
                    "cover-6k.htm",
                    "earnings-8k.htm",
                    "director-8k.htm",
                    "old-10k.htm",
                ],
                "primaryDocDescription": [
                    "10-Q",
                    "6-K Cover",
                    "8-K Results",
                    "8-K Director",
                    "10-K",
                ],
                "items": [[], [], ["2.02"], ["5.02"], []],
            }
        },
    }
    index_6k_payload = {
        "directory": {
            "item": [
                {"name": "cover-6k.htm", "size": "1200"},
                {"name": "ex99-1-earnings-release.htm", "size": "48500"},
                {"name": "index.json", "size": "800"},
            ]
        }
    }
    companyfacts_payload = {
        "cik": 123456,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "val": 22594000000,
                                "end": "2025-12-31",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-20",
                                "accn": "0000123456-26-000001",
                            },
                            {
                                "val": 19800000000,
                                "end": "2024-12-31",
                                "fy": 2024,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2025-02-20",
                                "accn": "0000123456-25-000001",
                            },
                        ]
                    }
                }
            }
        },
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("User-Agent") == SEC_UA
        url = str(request.url)
        if url.endswith("CIK0000123456.json") and "submissions" in url:
            return httpx.Response(200, json=submissions_payload)
        if url.endswith("/000012345626000020/index.json"):
            return httpx.Response(200, json=index_6k_payload)
        if url.endswith("companyfacts/CIK0000123456.json"):
            return httpx.Response(200, json=companyfacts_payload)
        return httpx.Response(404)

    adapter = SecEdgarAdapter(
        user_agent=SEC_UA,
        backfill_years=3,
        transport=httpx.MockTransport(_handler),
    )
    company = CompanyTarget(
        source_key="example-us", ticker="EXMP", exchange="XNAS", name="Example US", sec_cik=123456
    )
    cands = await adapter.discover(company, today=date(2026, 11, 26))

    # Keeps 10-Q, 6-K (EX-99.1 exhibit, NOT cover-6k.htm), and 8-K (Item 2.02).
    # Rejects 8-K (Item 5.02) and 2020 10-K (outside backfill).
    assert len(cands) == 3
    by_form = {c.form_type: c for c in cands}
    assert by_form["10-Q"].source_url.endswith("/q2-2026.htm")
    assert by_form["6-K"].source_url.endswith("/ex99-1-earnings-release.htm")
    assert by_form["8-K"].source_url.endswith("/earnings-8k.htm")

    facts = await adapter.fetch_company_facts(123456)
    rows = query_facts(facts, ["Revenues"], periods=["FY2025"])
    assert len(rows) == 1
    assert rows[0]["val"] == 22594000000
    assert rows[0]["fy"] == 2025


@pytest.mark.asyncio
async def test_hkexnews_noise_rejection_and_stock_id_resolution() -> None:
    # Direct rule checks
    assert is_kept_hkex_filing("40000", "40100", "Annual Report 2025") == (
        True,
        FormFamily.ANNUAL_REPORT,
    )
    assert is_kept_hkex_filing("10000", "13300", "Annual Results Announcement") == (
        True,
        FormFamily.RESULTS_RELEASE,
    )
    assert is_kept_hkex_filing("10000", "16400", "Monthly Return of Equity Issuer")[0] is False
    assert is_kept_hkex_filing("10000", "16300", "Next Day Disclosure Return")[0] is False
    assert is_kept_hkex_filing("10000", "13100", "Date of Board Meeting")[0] is False
    assert is_kept_hkex_filing("19000", "19100", "Overseas Regulatory Announcement")[0] is False

    search_records = [
        {
            "NEWS_ID": "1001",
            "t1_code": "40000",
            "t2_code": "40100",
            "TITLE": "Annual Report 2025",
            "DATE_TIME": "20/04/2026 17:30",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0420/ar2025.pdf",
            "SHORT_TEXT": "Annual Report",
        },
        {
            "NEWS_ID": "1002",
            "t1_code": "10000",
            "t2_code": "16400",
            "TITLE": "Monthly Return of Equity Issuer on Movements in Securities",
            "DATE_TIME": "01/05/2026 17:00",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0501/monthly.pdf",
            "SHORT_TEXT": "Monthly Return",
        },
        {
            "NEWS_ID": "1003",
            "t1_code": "10000",
            "t2_code": "16300",
            "TITLE": "Next Day Disclosure Return",
            "DATE_TIME": "02/05/2026 18:00",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0502/nddr.pdf",
            "SHORT_TEXT": "Next Day Disclosure Return",
        },
        {
            "NEWS_ID": "1004",
            "t1_code": "10000",
            "t2_code": "13100",
            "TITLE": "Date of Board Meeting",
            "DATE_TIME": "03/05/2026 18:00",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0503/board.pdf",
            "SHORT_TEXT": "Board Meeting",
        },
        {
            "NEWS_ID": "1005",
            "t1_code": "19000",
            "t2_code": "19100",
            "TITLE": "Overseas Regulatory Announcement",
            "DATE_TIME": "04/05/2026 18:00",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0504/ora.pdf",
            "SHORT_TEXT": "Overseas Regulatory Announcement",
        },
        {
            "NEWS_ID": "1006",
            "t1_code": "10000",
            "t2_code": "13400",
            "TITLE": "Interim Results Announcement for Six Months Ended June 30, 2026",
            "DATE_TIME": "18/08/2026 17:05",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0818/interim.pdf",
            "SHORT_TEXT": "Interim Results",
        },
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        if "activestock_sehk_e.json" in str(request.url):
            return httpx.Response(200, json=[{"i": "99901", "c": "00700", "n": "Example HK"}])
        if "titleSearchServlet.do" in str(request.url):
            assert request.url.params.get("stockId") == "99901"
            assert request.url.params.get("lang") == "E"
            return httpx.Response(200, json={"result": json.dumps(search_records)})
        return httpx.Response(404)

    adapter = HkexnewsAdapter(backfill_years=3, transport=httpx.MockTransport(_handler))
    company = CompanyTarget(
        source_key="example-hk", ticker="0700.HK", exchange="XHKG", name="Example HK"
    )
    cands = await adapter.discover(company, today=date(2026, 11, 26))
    assert [c.external_id for c in cands] == ["1001", "1006"]
    assert [c.form_family for c in cands] == [FormFamily.ANNUAL_REPORT, FormFamily.RESULTS_RELEASE]


@pytest.mark.asyncio
async def test_esef_xbrl_and_earnings_watch_adapters() -> None:
    esef_payload = {
        "data": [
            {
                "id": "fxo-2025",
                "attributes": {
                    "fxo_id": "549300TESTLEI0000001-2025-12-31",
                    "period_end": "2025-12-31",
                    "date_added": "2026-03-20T10:00:00Z",
                    "country": "DE",
                    "report_url": "/549300TESTLEI0000001/2025-12-31/ESEF/DE/0/report.xhtml",
                },
            },
            {
                "id": "fxo-2019",
                "attributes": {
                    "fxo_id": "549300TESTLEI0000001-2019-12-31",
                    "period_end": "2019-12-31",
                    "date_added": "2020-03-20T10:00:00Z",
                    "country": "DE",
                    "report_url": "/549300TESTLEI0000001/2019-12-31/ESEF/DE/0/report.xhtml",
                },
            },
        ]
    }

    def _esef_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("filter[entity.identifier]") == "549300TESTLEI0000001"
        return httpx.Response(200, json=esef_payload)

    eu_company = CompanyTarget(
        source_key="example-eu",
        ticker="EXEU",
        exchange="XETR",
        name="Example EU AG",
        lei="549300TESTLEI0000001",
        verified_documents=[
            VerifiedDocumentHint(
                id="ew-release-1",
                event_id="evt-1",
                url="https://ir.example.test/q3-2026-release.html",
                title="Q3 2026 Earnings Release",
                role="results_release",
                first_seen="2026-10-28T07:00:00Z",
            )
        ],
    )

    esef = EsefXbrlAdapter(backfill_years=3, transport=httpx.MockTransport(_esef_handler))
    esef_cands = await esef.discover(eu_company, today=date(2026, 11, 26))
    assert len(esef_cands) == 1
    assert esef_cands[0].source_url == (
        "https://filings.xbrl.org/549300TESTLEI0000001/2025-12-31/ESEF/DE/0/report.xhtml"
    )

    ew = EarningsWatchAdapter()
    ew_cands = await ew.discover(eu_company, today=date(2026, 11, 26))
    assert len(ew_cands) == 1
    assert ew_cands[0].refetchable is False
    assert ew_cands[0].form_family == FormFamily.RESULTS_RELEASE


@pytest.mark.asyncio
async def test_targets_client_ttl_and_fail_closed_on_503(tmp_path: Path) -> None:
    token_file = tmp_path / "targets.token"
    token_file.write_text("test-bearer-secret\n", encoding="utf-8")
    token_file.chmod(0o600)

    state = {"status": 200, "calls": 0}
    now_t = {"t": 100.0}

    def _handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        assert request.headers.get("Authorization") == "Bearer test-bearer-secret"
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"error": "unavailable"})
        return httpx.Response(
            200,
            json={
                "schema": 1,
                "generated_at": "2026-11-26T00:00:00Z",
                "companies": [
                    {
                        "source_key": "example-us",
                        "ticker": "EXMP",
                        "exchange": "XNAS",
                        "name": "Example US",
                    }
                ],
                "scopes": [{"person": "alice", "source_keys": ["example-us"]}],
            },
        )

    client = LibraryTargetsClient(
        base_url="https://hub.example.test",
        token_file=token_file,
        ttl_seconds=60.0,
        transport=httpx.MockTransport(_handler),
        clock=lambda: now_t["t"],
    )

    # Initial fetch succeeds and is cached within TTL.
    keys = await client.allowed_source_keys_for("alice")
    assert keys == {"example-us"}
    assert state["calls"] == 1
    now_t["t"] = 130.0
    assert await client.allowed_source_keys_for("alice") == {"example-us"}
    assert state["calls"] == 1

    # After TTL expires and hub returns 503, client MUST fail closed (never return stale holdings).
    now_t["t"] = 165.0
    state["status"] = 503
    with pytest.raises(HubUnavailableError):
        await client.allowed_source_keys_for("alice")

    # TTL > 60s is rejected at construction.
    with pytest.raises(ValueError):
        LibraryTargetsClient(base_url="https://hub.example.test", token="x", ttl_seconds=61.0)


@pytest.mark.asyncio
async def test_end_to_end_sync_once_and_export_markdown(tmp_path: Path) -> None:
    store = FilingsStore(tmp_path / "library.sqlite3")
    cas = ContentAddressedStore(tmp_path / "cas")

    targets_payload = {
        "schema": 1,
        "generated_at": "2026-11-26T00:00:00Z",
        "companies": [
            {
                "source_key": "example-us",
                "ticker": "EXMP",
                "exchange": "XNAS",
                "name": "Example US",
                "sec_cik": 123456,
                "verified_documents": [
                    {
                        "id": "ew-1",
                        "event_id": "ev-1",
                        "url": "https://ir.example.test/release.html",
                        "title": "Q3 2026 Results Release",
                        "role": "results_release",
                        "first_seen": "2026-11-01T08:00:00Z",
                    }
                ],
            }
        ],
        "scopes": [{"person": "alice", "source_keys": ["example-us"]}],
    }

    sec_submissions = {
        "cik": "0000123456",
        "filings": {
            "recent": {
                "form": ["10-K", "10-K/A"],
                "accessionNumber": ["0000123456-26-000001", "0000123456-26-000002"],
                "filingDate": ["2026-02-20", "2026-03-05"],
                "reportDate": ["2025-12-31", "2025-12-31"],
                "primaryDocument": ["ar2025.htm", "ar2025-amend.htm"],
                "primaryDocDescription": ["Annual Report 2025", "Annual Report 2025 Amendment"],
                "items": [[], []],
            }
        },
    }
    sec_facts: dict[str, Any] = {
        "cik": 123456,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [{"val": 22594000000, "end": "2025-12-31", "fy": 2025, "fp": "FY"}]
                    }
                }
            }
        },
    }

    def _web_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/internal/library-targets"):
            return httpx.Response(200, json=targets_payload)
        if "submissions/CIK0000123456.json" in url:
            return httpx.Response(200, json=sec_submissions)
        if "companyfacts/CIK0000123456.json" in url:
            return httpx.Response(200, json=sec_facts)
        if url.endswith("/ar2025.htm"):
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"<html><body><h2>Item 7. MD&A</h2><p>Original revenue $22,594M.</p></body></html>",
            )
        if url.endswith("/ar2025-amend.htm"):
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"<html><body><h2>Item 7. MD&A</h2><p>Restated revenue $22,610M.</p></body></html>",
            )
        if url.endswith("/release.html"):
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"<html><body><h2>Q3 Highlights</h2><p>Strong cloud demand.</p></body></html>",
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_web_handler)
    targets_client = LibraryTargetsClient(
        base_url="https://hub.example.test",
        token="sync-token",
        transport=transport,
    )
    extractor_client = ExtractorClient.in_process()

    res1 = await sync_once(
        store=store,
        cas=cas,
        targets_client=targets_client,
        extractor_client=extractor_client,
        config=SyncConfig(sec_user_agent=SEC_UA),
        download_transport=transport,
        today=date(2026, 11, 26),
    )
    assert res1.errors == []
    assert res1.companies_synced == 1
    assert res1.documents_ingested == 3
    assert res1.facts_updated == 1

    # Verify amendment supersession link was wired automatically.
    docs = store.list_documents({"example-us"})
    by_form = {d.form_type: d for d in docs}
    assert by_form["10-K/A"].is_amendment is True
    assert by_form["10-K/A"].supersedes_document_id == by_form["10-K"].document_id
    assert by_form["results_release"].refetchable is False

    # Verify search hits normalized numeric value and ranks 10-K/A above superseded 10-K.
    hits = search_filings(store, "22610", allowed_source_keys={"example-us"})
    assert len(hits) == 1
    assert hits[0].form_type == "10-K/A"

    # Second sync pass is idempotent (skips existing docs).
    res2 = await sync_once(
        store=store,
        cas=cas,
        targets_client=targets_client,
        extractor_client=extractor_client,
        config=SyncConfig(sec_user_agent=SEC_UA),
        download_transport=transport,
        today=date(2026, 11, 26),
    )
    assert res2.documents_ingested == 0
    assert res2.documents_skipped_existing == 3

    # One-way disposable Markdown export.
    exported = export_markdown(store, cas, tmp_path / "export", allowed_source_keys={"example-us"})
    assert len(exported) == 3
    assert all(p.is_file() and p.suffix == ".md" for p in exported)
    store.close()
