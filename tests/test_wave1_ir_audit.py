"""Fail-first behavioral tests for Wave 1 (Slices 1 + 2) IR audit fixes."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from filings_library.adapters.earnings_watch import EarningsWatchAdapter
from filings_library.adapters.esef_xbrl import EsefXbrlAdapter
from filings_library.adapters.hkexnews import HkexnewsAdapter, is_kept_hkex_filing
from filings_library.adapters.sec_edgar import SecEdgarAdapter
from filings_library.cas import ContentAddressedStore
from filings_library.extractor.app import ExtractorClient
from filings_library.models import (
    CompanyTarget,
    DocumentRecord,
    ExtractedSection,
    FormFamily,
    LibraryTargetsPayload,
    SourceAdapter,
    VerifiedDocumentHint,
)
from filings_library.store import FilingsStore
from filings_library.targets_client import LibraryTargetsClient
from filings_library.worker import SyncConfig, sync_once

SEC_UA = "FilingsLibraryTest/0.1 (contact@example.test)"


def _public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def test_startup_migration_purges_issuer_ir_and_ir_gap_backfill_idempotently(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "filings.sqlite3"
    cas = ContentAddressedStore(tmp_path)
    store = FilingsStore(db_path)

    company = CompanyTarget(
        source_key="example-us", ticker="EXMP", exchange="XNAS", name="Example US", sec_cik=123456
    )
    store.upsert_company(company)

    ir_sha = cas.put_bytes(b"legacy issuer_ir payload")
    sec_sha = cas.put_bytes(b"valid sec payload")

    # Insert a valid sec_edgar doc + section/FTS
    store.upsert_document(
        DocumentRecord(
            document_id="doc-sec-1",
            source_key="example-us",
            adapter=SourceAdapter.SEC_EDGAR,
            external_id="accn-1:10k.htm",
            form_type="10-K",
            form_family=FormFamily.ANNUAL_REPORT,
            title="Annual Report 2025",
            filing_date="2026-02-20",
            source_url="https://www.sec.gov/Archives/edgar/data/123456/0001/10k.htm",
            source_sha256=sec_sha,
            source_mime="text/html",
            source_bytes=len(b"valid sec payload"),
        )
    )
    store.replace_sections(
        "doc-sec-1",
        [ExtractedSection(locator="section:1", title="Item 1", ordinal=0, markdown="SEC text")],
    )

    # Seed legacy issuer_ir doc directly in SQLite (simulating pre-migration DB)
    store.conn.execute(
        """
        INSERT INTO documents(
            document_id, source_key, adapter, external_id, form_type, form_family,
            title, filing_date, language, source_url, source_sha256, source_mime,
            source_bytes, refetchable, is_amendment, created_at
        ) VALUES (?, ?, 'issuer_ir', ?, 'presentation', 'presentation',
                  'Legacy Deck', '2026-01-10', 'en', 'https://ir.example.test/deck.pdf',
                  ?, 'application/pdf', 24, 0, 0, '2026-01-10T00:00:00Z')
        """,
        ("doc-ir-1", "example-us", "ir-1", ir_sha),
    )
    store.conn.execute(
        """
        INSERT INTO sections(document_id, locator, title, ordinal, markdown, ocr_used)
        VALUES ('doc-ir-1', 'page:1', 'Slide 1', 0, 'Legacy IR slide', 0)
        """
    )
    store.conn.execute(
        """
        INSERT INTO fts_chunks(title, body, normalized_numbers, document_id, source_key, locator)
        VALUES ('Slide 1', 'Legacy IR slide', '', 'doc-ir-1', 'example-us', 'page:1')
        """
    )
    store.enqueue_job("ir_gap_backfill", {"source_key": "example-us", "ticker": "EXMP"})
    leased_job_id = store.enqueue_job(
        "ir_gap_backfill", {"source_key": "example-us", "ticker": "EXMP2"}
    )
    store.conn.execute("UPDATE jobs SET status = 'leased' WHERE id = ?", (leased_job_id,))
    store.enqueue_job("audio_transcribe", {"source_key": "example-us", "external_id": "a1"})
    store.close()

    # Re-open triggers idempotent startup migration
    store2 = FilingsStore(db_path)
    assert store2.get_document("doc-ir-1") is None
    assert store2.get_sections("doc-ir-1") == []
    fts_ir = store2.conn.execute(
        "SELECT COUNT(*) AS cnt FROM fts_chunks WHERE document_id = 'doc-ir-1'"
    ).fetchone()
    assert int(fts_ir["cnt"]) == 0

    # Other adapter rows, audio_transcribe job, and CAS objects are untouched
    assert store2.get_document("doc-sec-1") is not None
    assert len(store2.get_sections("doc-sec-1")) == 1
    fts_sec = store2.conn.execute(
        "SELECT COUNT(*) AS cnt FROM fts_chunks WHERE document_id = 'doc-sec-1'"
    ).fetchone()
    assert int(fts_sec["cnt"]) == 1
    assert cas.has(ir_sha)
    assert cas.has(sec_sha)

    gap_jobs = store2.conn.execute(
        "SELECT COUNT(*) AS cnt FROM jobs WHERE kind = 'ir_gap_backfill'"
    ).fetchone()
    assert int(gap_jobs["cnt"]) == 0
    asr_jobs = store2.conn.execute(
        "SELECT COUNT(*) AS cnt FROM jobs WHERE kind = 'audio_transcribe'"
    ).fetchone()
    assert int(asr_jobs["cnt"]) == 1
    store2.close()

    # Second re-open is idempotent
    store3 = FilingsStore(db_path)
    assert store3.get_document("doc-sec-1") is not None
    store3.close()


@pytest.mark.asyncio
async def test_sec_8k_item_202_extracts_ex99_1_and_caches_index_json(tmp_path: Path) -> None:
    store = FilingsStore(tmp_path / "filings.sqlite3")
    index_fetches = 0

    submissions_payload = {
        "cik": "0000123456",
        "filings": {
            "recent": {
                "form": ["8-K"],
                "accessionNumber": ["0000123456-26-000099"],
                "filingDate": ["2026-10-28"],
                "reportDate": ["2026-09-30"],
                "primaryDocument": ["form8k.htm"],
                "primaryDocDescription": ["8-K Results"],
                "items": [["2.02", "9.01"]],
            }
        },
    }
    index_payload = {
        "directory": {
            "item": [
                {"name": "form8k.htm", "size": "2500"},
                {"name": "ex99-1-q3-earnings.htm", "size": "62000"},
                {"name": "index.json", "size": "900"},
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal index_fetches
        url = str(request.url)
        if url.endswith("CIK0000123456.json"):
            return httpx.Response(200, json=submissions_payload)
        if url.endswith("/000012345626000099/index.json"):
            index_fetches += 1
            return httpx.Response(200, json=index_payload)
        return httpx.Response(404)

    adapter = SecEdgarAdapter(
        user_agent=SEC_UA,
        backfill_years=3,
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
        store=store,
    )
    company = CompanyTarget(
        source_key="example-us", ticker="EXMP", exchange="XNAS", name="Example US", sec_cik=123456
    )

    cands1 = await adapter.discover(company, today=date(2026, 11, 26))
    assert index_fetches == 1
    by_ext = {c.external_id: c for c in cands1}
    assert "0000123456-26-000099:ex99-1-q3-earnings.htm" in by_ext
    ex99 = by_ext["0000123456-26-000099:ex99-1-q3-earnings.htm"]
    assert ex99.form_family == FormFamily.RESULTS_RELEASE
    assert "0000123456-26-000099:form8k.htm" in by_ext
    assert by_ext["0000123456-26-000099:form8k.htm"].form_family == FormFamily.CURRENT_REPORT

    # Second discover (even with a fresh adapter instance) must hit SQLite cache
    adapter2 = SecEdgarAdapter(
        user_agent=SEC_UA,
        backfill_years=3,
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
        store=store,
    )
    cands2 = await adapter2.discover(company, today=date(2026, 11, 26))
    assert len(cands2) == len(cands1)
    assert index_fetches == 1
    store.close()


@pytest.mark.asyncio
async def test_sec_6k_routine_noise_classification_and_store_migration(tmp_path: Path) -> None:
    submissions_payload = {
        "cik": "0000654321",
        "filings": {
            "recent": {
                "form": ["6-K", "6-K", "6-K", "6-K", "6-K", "6-K"],
                "accessionNumber": [
                    "0000654321-26-000001",
                    "0000654321-26-000002",
                    "0000654321-26-000003",
                    "0000654321-26-000004",
                    "0000654321-26-000005",
                    "0000654321-26-000006",
                ],
                "filingDate": [
                    "2026-10-01",
                    "2026-10-02",
                    "2026-10-03",
                    "2026-10-04",
                    "2026-10-05",
                    "2026-10-20",
                ],
                "reportDate": [
                    "2026-10-01",
                    "2026-10-02",
                    "2026-10-03",
                    "2026-10-04",
                    "2026-10-05",
                    "2026-09-30",
                ],
                "primaryDocument": [
                    "buyback.htm",
                    "tvr.htm",
                    "pdmr.htm",
                    "block.htm",
                    "holdings.htm",
                    "q3-results.htm",
                ],
                "primaryDocDescription": [
                    "Transaction in Own Shares",
                    "Total Voting Rights",
                    "Director/PDMR Shareholding",
                    "Block Listing Six Monthly Return",
                    "Holding(s) in Company",
                    "Q3 2026 Financial Results",
                ],
                "items": [[], [], [], [], [], []],
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("CIK0000654321.json"):
            return httpx.Response(200, json=submissions_payload)
        return httpx.Response(404)

    adapter = SecEdgarAdapter(
        user_agent=SEC_UA,
        backfill_years=3,
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )
    company = CompanyTarget(
        source_key="example-adr", ticker="EADR", exchange="XNYS", name="Example ADR", sec_cik=654321
    )
    cands = await adapter.discover(company, today=date(2026, 11, 26))
    by_title = {c.title: c for c in cands}
    assert by_title["Transaction in Own Shares"].form_family == FormFamily.CURRENT_REPORT
    assert by_title["Total Voting Rights"].form_family == FormFamily.CURRENT_REPORT
    assert by_title["Director/PDMR Shareholding"].form_family == FormFamily.CURRENT_REPORT
    assert by_title["Block Listing Six Monthly Return"].form_family == FormFamily.CURRENT_REPORT
    assert by_title["Holding(s) in Company"].form_family == FormFamily.CURRENT_REPORT
    assert by_title["Q3 2026 Financial Results"].form_family == FormFamily.RESULTS_RELEASE

    # Verify idempotent SQLite migration reclassifies existing routine 6-K rows
    db_path = tmp_path / "migrate6k.sqlite3"
    store = FilingsStore(db_path)
    store.upsert_company(company)
    store.upsert_document(
        DocumentRecord(
            document_id="doc-6k-buyback",
            source_key="example-adr",
            adapter=SourceAdapter.SEC_EDGAR,
            external_id="0000654321-26-000001:buyback.htm",
            form_type="6-K",
            form_family=FormFamily.RESULTS_RELEASE,
            title="Transaction in Own Shares",
            filing_date="2026-10-01",
            source_url="https://www.sec.gov/Archives/edgar/data/654321/000065432126000001/buyback.htm",
            source_sha256="a" * 64,
            source_mime="text/html",
            source_bytes=100,
        )
    )
    store.upsert_document(
        DocumentRecord(
            document_id="doc-6k-results",
            source_key="example-adr",
            adapter=SourceAdapter.SEC_EDGAR,
            external_id="0000654321-26-000006:q3-results.htm",
            form_type="6-K",
            form_family=FormFamily.RESULTS_RELEASE,
            title="Q3 2026 Financial Results",
            filing_date="2026-10-20",
            source_url="https://www.sec.gov/Archives/edgar/data/654321/000065432126000006/q3-results.htm",
            source_sha256="b" * 64,
            source_mime="text/html",
            source_bytes=100,
        )
    )
    store.close()

    store2 = FilingsStore(db_path)
    doc_bb = store2.get_document("doc-6k-buyback")
    doc_res = store2.get_document("doc-6k-results")
    assert doc_bb is not None and doc_bb.form_family == FormFamily.CURRENT_REPORT
    assert doc_res is not None and doc_res.form_family == FormFamily.RESULTS_RELEASE
    store2.close()


@pytest.mark.asyncio
async def test_per_issuer_max_docs_prioritizes_financial_filings_and_preserves_supersession(
    tmp_path: Path,
) -> None:
    store = FilingsStore(tmp_path / "filings.sqlite3")
    cas = ContentAddressedStore(tmp_path)

    submissions_payload = {
        "cik": "0000123456",
        "filings": {
            "recent": {
                "form": ["8-K", "8-K", "8-K", "10-K", "10-K/A"],
                "accessionNumber": [
                    "0000123456-24-000001",
                    "0000123456-24-000002",
                    "0000123456-24-000003",
                    "0000123456-26-000010",
                    "0000123456-26-000011",
                ],
                "filingDate": [
                    "2024-01-10",
                    "2024-02-10",
                    "2024-03-10",
                    "2026-02-20",
                    "2026-03-05",
                ],
                "reportDate": [
                    "2024-01-10",
                    "2024-02-10",
                    "2024-03-10",
                    "2025-12-31",
                    "2025-12-31",
                ],
                "primaryDocument": [
                    "k1.htm",
                    "k2.htm",
                    "k3.htm",
                    "ar2025.htm",
                    "ar2025-amend.htm",
                ],
                "primaryDocDescription": [
                    "Old 8-K 1",
                    "Old 8-K 2",
                    "Newer 8-K 3",
                    "Annual Report 2025",
                    "Annual Report 2025 Amendment",
                ],
                "items": [["8.01"], ["8.01"], ["8.01"], [], []],
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("CIK0000123456.json") and "submissions" in url:
            return httpx.Response(200, json=submissions_payload)
        if url.endswith("companyfacts/CIK0000123456.json"):
            return httpx.Response(200, json={"cik": 123456, "facts": {}})
        if url.endswith(".htm"):
            body = f"<html><body><h1>{url}</h1><p>Filing body {url}</p></body></html>"
            return httpx.Response(
                200, content=body.encode("utf-8"), headers={"content-type": "text/html"}
            )
        return httpx.Response(404)

    payload = LibraryTargetsPayload(
        schema=2,
        generated_at="2026-11-27T00:00:00Z",
        companies=[
            CompanyTarget(
                source_key="example-us",
                ticker="EXMP",
                exchange="XNAS",
                name="Example US",
                sec_cik=123456,
            )
        ],
        scopes=[],
    )

    class _StaticTargets(LibraryTargetsClient):
        async def fetch_targets(self, *, force_refresh: bool = False) -> LibraryTargetsPayload:
            del force_refresh
            return payload

    res = await sync_once(
        store=store,
        cas=cas,
        targets_client=_StaticTargets(base_url="http://targets.local", token="t" * 40),
        extractor_client=ExtractorClient.in_process(),
        config=SyncConfig(per_issuer_max_docs=3, sec_user_agent=SEC_UA),
        download_transport=httpx.MockTransport(handler),
        url_resolver=_public_resolver,
        today=date(2026, 11, 26),
    )
    assert res.errors == []
    assert res.documents_ingested == 3
    assert res.documents_skipped_quota == 2

    docs = store.list_documents({"example-us"})
    by_form = {d.form_type: d for d in docs}
    assert "10-K" in by_form
    assert "10-K/A" in by_form
    assert by_form["10-K/A"].supersedes_document_id == by_form["10-K"].document_id
    # The single remaining slot goes to the newest 8-K (2024-03-10), not oldest
    assert by_form["8-K"].filing_date == "2024-03-10"
    store.close()


@pytest.mark.asyncio
async def test_cross_adapter_sha256_and_url_deduplication(tmp_path: Path) -> None:
    store = FilingsStore(tmp_path / "filings.sqlite3")
    cas = ContentAddressedStore(tmp_path)

    shared_html = b"<html><body><h1>Q3 2026 Earnings</h1><p>Revenue $900M.</p></body></html>"
    submissions_payload = {
        "cik": "0000123456",
        "filings": {
            "recent": {
                "form": ["10-Q", "8-K"],
                "accessionNumber": ["0000123456-26-000050", "0000123456-26-000051"],
                "filingDate": ["2026-10-30", "2026-10-31"],
                "reportDate": ["2026-09-30", "2026-10-31"],
                "primaryDocument": ["q3.htm", "same-bytes.htm"],
                "primaryDocDescription": ["10-Q", "8-K"],
                "items": [[], ["8.01"]],
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("CIK0000123456.json") and "submissions" in url:
            return httpx.Response(200, json=submissions_payload)
        if url.endswith("companyfacts/CIK0000123456.json"):
            return httpx.Response(200, json={"cik": 123456, "facts": {}})
        if request.url.path.endswith((".htm", ".html")):
            return httpx.Response(200, content=shared_html, headers={"content-type": "text/html"})
        return httpx.Response(404)

    payload = LibraryTargetsPayload(
        schema=2,
        generated_at="2026-11-27T00:00:00Z",
        companies=[
            CompanyTarget(
                source_key="example-us",
                ticker="EXMP",
                exchange="XNAS",
                name="Example US",
                sec_cik=123456,
                verified_documents=[
                    # Same canonical URL as SEC 10-Q (with fragment + uppercase host)
                    VerifiedDocumentHint(
                        id="ew-dup-url",
                        url="https://WWW.SEC.GOV/Archives/edgar/data/123456/000012345626000050/q3.htm#top",
                        title="Q3 2026 Filing",
                        role="quarterly_report",
                        published_date="2026-10-30",
                    ),
                    # Different URL, identical SHA-256 content
                    VerifiedDocumentHint(
                        id="ew-dup-sha",
                        url="https://ir.example.test/q3-copy.html",
                        title="Q3 2026 Copy",
                        role="results_release",
                        published_date="2026-10-30",
                    ),
                ],
            )
        ],
        scopes=[],
    )

    class _StaticTargets(LibraryTargetsClient):
        async def fetch_targets(self, *, force_refresh: bool = False) -> LibraryTargetsPayload:
            del force_refresh
            return payload

    res = await sync_once(
        store=store,
        cas=cas,
        targets_client=_StaticTargets(base_url="http://targets.local", token="t" * 40),
        extractor_client=ExtractorClient.in_process(),
        config=SyncConfig(sec_user_agent=SEC_UA),
        download_transport=httpx.MockTransport(handler),
        url_resolver=_public_resolver,
        today=date(2026, 11, 26),
    )
    assert res.errors == []
    # All 4 candidates resolve to the same content/URL -> only 1 document stored
    assert res.documents_ingested == 1
    assert store.count_documents_for_issuer("example-us") == 1
    store.close()


@pytest.mark.asyncio
async def test_hkexnews_keeps_multitag_overseas_results_and_cleans_form_type() -> None:
    kept, family = is_kept_hkex_filing(
        "",
        "",
        "Third Quarterly Report 2026",
        long_text="Quarterly Results<br/>Inside Information<br/>Overseas Regulatory Announcement - Trading Update",
    )
    assert kept is True
    assert family in {FormFamily.RESULTS_RELEASE, FormFamily.QUARTERLY_REPORT}

    kept_fs, family_fs = is_kept_hkex_filing(
        "",
        "",
        "Quarterly Report 2026",
        long_text="Financial Statements&#x2f;ESG Information<br/>Quarterly Report<br/>Overseas Regulatory Announcement",
    )
    assert kept_fs is True
    assert family_fs == FormFamily.QUARTERLY_REPORT

    # Pure overseas regulatory announcement without results category is still rejected
    assert is_kept_hkex_filing("", "", "Notice", long_text="Overseas Regulatory Announcement") == (
        False,
        FormFamily.OTHER,
    )

    search_records = [
        {
            "NEWS_ID": "9001",
            "TITLE": "Q3 2026 Results Announcement",
            "DATE_TIME": "25/10/2026 18:00",
            "FILE_LINK": "/listedco/listconews/sehk/2026/1025/q3.pdf",
            "SHORT_TEXT": "Headline Category: <div class='cat'>Results&#x2f;Quarterly</div><br/>Overseas Regulatory Announcement",
            "LONG_TEXT": "Quarterly Results<br/>Inside Information<br/>Overseas Regulatory Announcement - Trading Update",
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "titleSearchServlet.do" in str(request.url):
            return httpx.Response(200, json={"result": search_records})
        return httpx.Response(404)

    adapter = HkexnewsAdapter(
        backfill_years=3,
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )
    company = CompanyTarget(
        source_key="example-hk",
        ticker="09999",
        exchange="XHKG",
        name="Example HK",
        hkex_stock_id="99999",
    )
    cands = await adapter.discover(company, today=date(2026, 11, 26))
    assert len(cands) == 1
    assert "<" not in cands[0].form_type
    assert "&#x2f;" not in cands[0].form_type
    assert "Results/Quarterly" in cands[0].form_type


@pytest.mark.asyncio
async def test_esef_language_detection_and_period_language_dedup() -> None:
    esef_payload = {
        "data": [
            {
                "id": "101",
                "attributes": {
                    "fxo_id": "549300TESTLEI0000001/2025-12-31/ESEF/DE/0",
                    "report_url": "/549300TESTLEI0000001/2025-12-31/ESEF/DE/0/report-de.xhtml",
                    "period_end": "2025-12-31",
                    "date_added": "2026-03-10T10:00:00Z",
                    "country": "DE",
                },
            },
            {
                "id": "102",
                "attributes": {
                    "fxo_id": "549300TESTLEI0000001/2025-12-31/ESEF/DE/1",
                    "report_url": "/549300TESTLEI0000001/2025-12-31/ESEF/DE/1/report-de.xhtml",
                    "period_end": "2025-12-31",
                    "date_added": "2026-03-15T10:00:00Z",
                    "country": "DE",
                },
            },
            {
                "id": "103",
                "attributes": {
                    "fxo_id": "549300TESTLEI0000001/2025-12-31/ESEF/IT/0",
                    "report_url": "/549300TESTLEI0000001/2025-12-31/ESEF/IT/0/bilancio-it.html",
                    "period_end": "2025-12-31",
                    "date_added": "2026-03-12T10:00:00Z",
                    "country": "IT",
                },
            },
            {
                "id": "104",
                "attributes": {
                    "fxo_id": "549300TESTLEI0000001/2025-12-31/ESEF/IT/1",
                    "report_url": "/549300TESTLEI0000001/2025-12-31/ESEF/IT/1/bilancio-it.html",
                    "period_end": "2025-12-31",
                    "date_added": "2026-03-12T10:00:00Z",
                    "country": "IT",
                },
            },
            {
                "id": "105",
                "attributes": {
                    "fxo_id": "549300TESTLEI0000001/2025-12-31/ESEF/FR/0",
                    "report_url": "/549300TESTLEI0000001/2025-12-31/ESEF/FR/0/rapport-fr.xhtml",
                    "period_end": "2025-12-31",
                    "date_added": "2026-03-11T10:00:00Z",
                    "country": "FR",
                },
            },
            {
                "id": "106",
                "attributes": {
                    "fxo_id": "549300TESTLEI0000001/2025-12-31/ESEF/GB/0",
                    "report_url": "/549300TESTLEI0000001/2025-12-31/ESEF/GB/0/company-2025-ar.xhtml",
                    "period_end": "2025-12-31",
                    "date_added": "2026-03-11T10:00:00Z",
                    "country": "GB",
                },
            },
        ]
    }

    adapter = EsefXbrlAdapter(
        backfill_years=3,
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, json=esef_payload)),
        resolver=_public_resolver,
    )
    company = CompanyTarget(
        source_key="example-eu",
        ticker="EXEU",
        exchange="XETR",
        name="Example EU",
        lei="549300TESTLEI0000001",
    )
    cands = await adapter.discover(company, today=date(2026, 11, 26))
    by_lang = {c.language: c for c in cands}
    assert len(cands) == 4
    assert set(by_lang.keys()) == {"de", "it", "fr", "en"}
    assert by_lang["de"].external_id.endswith("/ESEF/DE/1")
    assert by_lang["it"].external_id.endswith("/ESEF/IT/1")


@pytest.mark.asyncio
async def test_asx_adapter_markitdigital_and_html_fallback_and_worker(tmp_path: Path) -> None:
    from filings_library.adapters.asx import AsxAdapter
    from filings_library.url_safety import ASX_ALLOWED_HOSTS

    assert "asx.api.markitdigital.com" in ASX_ALLOWED_HOSTS
    assert "www.asx.com.au" in ASX_ALLOWED_HOSTS

    markit_payload = {
        "data": {
            "items": [
                {
                    "documentKey": "2924-02840001-3A648001",
                    "header": "Appendix 4E and Full Year Statutory Accounts",
                    "documentReleaseDate": "2026-08-20T08:30:00.000Z",
                },
                {
                    "documentKey": "2924-02840002-3A648002",
                    "header": "FY26 Investor Presentation",
                    "documentReleaseDate": "2026-08-20T08:35:00.000Z",
                },
                {
                    "documentKey": "2924-02840003-3A648003",
                    "header": "Appendix 3Y - Change of Director's Interest Notice",
                    "documentReleaseDate": "2026-08-21T09:00:00.000Z",
                },
                {
                    "documentKey": "2924-02840004-3A648004",
                    "header": "Daily share buy-back notice - Appendix 3E",
                    "documentReleaseDate": "2026-08-22T09:00:00.000Z",
                },
            ]
        }
    }

    adapter = AsxAdapter(
        backfill_years=3,
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, json=markit_payload)),
        resolver=_public_resolver,
    )
    company = CompanyTarget(
        source_key="example-au",
        ticker="EXA",
        exchange="XASX",
        name="Example Australia Ltd",
    )
    cands = await adapter.discover(company, today=date(2026, 11, 26))
    assert len(cands) == 2
    assert {c.form_family for c in cands} == {FormFamily.ANNUAL_REPORT, FormFamily.PRESENTATION}
    assert all(c.adapter == SourceAdapter.ASX for c in cands)

    # HTML fallback when MarkitDigital returns 503
    fallback_html = """
    <html><body><table>
      <tr>
        <td>18/02/2026<br/>08:45 AM</td>
        <td>Y</td>
        <td><a href="/asxpdf/20260218/pdf/01234567.pdf">Appendix 4D and Half-Year Report</a></td>
      </tr>
      <tr>
        <td>19/02/2026<br/>09:00 AM</td>
        <td>N</td>
        <td><a href="/asxpdf/20260219/pdf/01234568.pdf">Change of Director's Interest Notice</a></td>
      </tr>
    </table></body></html>
    """

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        if "asx.api.markitdigital.com" in str(request.url):
            return httpx.Response(503)
        if "www.asx.com.au" in str(request.url) and "year=2026" in str(request.url):
            return httpx.Response(200, text=fallback_html)
        return httpx.Response(200, text="<html><body><table></table></body></html>")

    adapter_fb = AsxAdapter(
        backfill_years=2,
        transport=httpx.MockTransport(fallback_handler),
        resolver=_public_resolver,
    )
    fb_cands = await adapter_fb.discover(company, today=date(2026, 11, 26))
    assert len(fb_cands) == 1
    assert fb_cands[0].form_family == FormFamily.INTERIM_REPORT
    assert fb_cands[0].filing_date == "2026-02-18"
    assert fb_cands[0].source_url == "https://www.asx.com.au/asxpdf/20260218/pdf/01234567.pdf"

    # End-to-end worker wiring for XASX issuer
    store = FilingsStore(tmp_path / "asx.sqlite3")
    cas = ContentAddressedStore(tmp_path)

    def sync_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "asx.api.markitdigital.com" in url:
            return httpx.Response(200, json=markit_payload)
        if "cdn-api.markitdigital.com" in url:
            return httpx.Response(
                200,
                content=b"<html><body><h1>ASX Filing</h1><p>FY26 Revenue $100M.</p></body></html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    class _AsxTargets(LibraryTargetsClient):
        async def fetch_targets(self, *, force_refresh: bool = False) -> LibraryTargetsPayload:
            del force_refresh
            return LibraryTargetsPayload(
                schema=2,
                generated_at="2026-11-27T00:00:00Z",
                companies=[company],
                scopes=[],
            )

    res = await sync_once(
        store=store,
        cas=cas,
        targets_client=_AsxTargets(base_url="http://targets.local", token="t" * 40),
        extractor_client=ExtractorClient.in_process(),
        config=SyncConfig(),
        download_transport=httpx.MockTransport(sync_handler),
        url_resolver=_public_resolver,
        today=date(2026, 11, 26),
    )
    assert res.errors == []
    assert res.documents_ingested == 1  # 2nd candidate has identical HTML bytes (SHA-256 deduped!)
    docs = store.list_documents({"example-au"})
    assert len(docs) == 1
    assert docs[0].adapter == SourceAdapter.ASX
    store.close()


@pytest.mark.asyncio
async def test_earnings_watch_blocks_non_audio_asr_and_preserves_published_date(
    tmp_path: Path,
) -> None:
    company = CompanyTarget(
        source_key="example-ir",
        ticker="EXIR",
        exchange="XNYS",
        name="Example IR Corp",
        ir_assets=[
            VerifiedDocumentHint(
                id="yt-1",
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title="Q3 Earnings Call Replay",
                role="audio_replay",
                category="audio_replay",
                published_date="2026-10-25",
                first_seen="2026-11-28T10:00:00Z",
            ),
            VerifiedDocumentHint(
                id="vimeo-1",
                url="https://vimeo.com/123456789",
                title="Q3 Webcast Video",
                role="audio_replay",
                category="audio_replay",
                published_date=None,
                first_seen="2026-11-28T10:00:00Z",
            ),
            VerifiedDocumentHint(
                id="hls-1",
                url="https://cdn.example.com/streams/q3-call/playlist.m3u8",
                title="Q3 HLS Stream",
                role="audio_replay",
                category="audio_replay",
                published_date="2026-10-25",
                first_seen="2026-11-28T10:00:00Z",
            ),
            VerifiedDocumentHint(
                id="mp3-1",
                url="https://cdn.example.com/audio/q3-call.mp3",
                title="Q3 Direct Audio",
                role="audio_replay",
                category="audio_replay",
                published_date="2026-10-25",
                first_seen="2026-11-28T10:00:00Z",
            ),
            VerifiedDocumentHint(
                id="undated-deck",
                url="https://cdn.example.com/decks/investor-day.pdf",
                title="Investor Day Deck",
                role="presentation",
                category="presentation",
                published_date=None,
                first_seen="2026-11-28T10:00:00Z",
            ),
        ],
    )

    ew = EarningsWatchAdapter(resolver=_public_resolver)
    cands = await ew.discover(company, today=date(2026, 11, 28))
    by_id = {c.external_id: c for c in cands}

    assert by_id["yt-1"].form_type == "webcast_replay"
    assert by_id["yt-1"].form_family == FormFamily.WEBCAST
    assert by_id["vimeo-1"].form_type == "webcast_replay"
    assert by_id["vimeo-1"].form_family == FormFamily.WEBCAST
    assert by_id["hls-1"].form_type == "webcast_replay"
    assert by_id["hls-1"].form_family == FormFamily.WEBCAST

    assert by_id["mp3-1"].form_type == "earnings_call_audio_replay"
    assert by_id["mp3-1"].form_family == FormFamily.TRANSCRIPT
    assert by_id["mp3-1"].filing_date == "2026-10-25"

    # Missing published_date on ir_asset must stay "undated", not fabricated from first_seen
    assert by_id["undated-deck"].filing_date == "undated"
    assert by_id["vimeo-1"].filing_date == "undated"
