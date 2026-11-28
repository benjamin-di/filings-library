"""Fail-first behavioral tests for Wave 2 (Slice 4) WAF HTTP 403 IR asset fallback."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from filings_library.adapters import FilingCandidate
from filings_library.adapters.asx import AsxAdapter
from filings_library.cas import ContentAddressedStore
from filings_library.extractor.app import ExtractorClient
from filings_library.models import (
    CompanyTarget,
    FormFamily,
    LibraryTargetsPayload,
    SourceAdapter,
    VerifiedDocumentHint,
)
from filings_library.store import FilingsStore
from filings_library.targets_client import HubUnavailableError, LibraryTargetsClient
from filings_library.worker import SyncConfig, sync_once

TEST_TOKEN = "t" * 40


def _public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.asyncio
async def test_sync_once_falls_back_to_targets_client_on_403_for_earnings_watch_ir_asset(
    tmp_path: Path,
) -> None:
    store = FilingsStore(tmp_path / "filings.sqlite3")
    cas = ContentAddressedStore(tmp_path / "objects")

    targets_payload = {
        "schema": 2,
        "generated_at": "2026-11-28T00:00:00Z",
        "companies": [
            {
                "source_key": "acme-waf",
                "ticker": "ACME",
                "exchange": "XNYS",
                "name": "Acme WAF Corp",
                "ir_assets": [
                    {
                        "id": "asset-waf-1",
                        "url": "https://ir.acme.example/releases/q3-2026.html",
                        "title": "Q3 2026 Results Release",
                        "role": "results_release",
                        "category": "results_release",
                        "published_date": "2026-11-20",
                        "period": "2026Q3",
                    }
                ],
            }
        ],
        "scopes": [{"person": "user-1", "source_keys": ["acme-waf"]}],
    }

    fallback_requests: list[httpx.Request] = []
    html_body = (
        b"<html><body><h1>Acme Q3 2026 Results</h1>"
        b"<p>Revenue increased 14 percent year over year.</p></body></html>"
    )

    def targets_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/library-targets":
            return httpx.Response(200, json=targets_payload)
        if request.url.path == "/internal/ir-assets/asset-waf-1/download":
            fallback_requests.append(request)
            assert request.headers.get("authorization") == f"Bearer {TEST_TOKEN}"
            return httpx.Response(
                200,
                content=html_body,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return httpx.Response(404)

    def direct_download_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://ir.acme.example/releases/q3-2026.html":
            return httpx.Response(403, text="WAF Challenge")
        return httpx.Response(404)

    targets_client = LibraryTargetsClient(
        base_url="http://targets.local",
        token=TEST_TOKEN,
        transport=httpx.MockTransport(targets_handler),
    )

    res = await sync_once(
        store=store,
        cas=cas,
        targets_client=targets_client,
        extractor_client=ExtractorClient.in_process(),
        config=SyncConfig(),
        download_transport=httpx.MockTransport(direct_download_handler),
        url_resolver=_public_resolver,
        today=date(2026, 11, 28),
    )

    assert res.errors == []
    assert res.documents_ingested == 1
    assert len(fallback_requests) == 1
    assert fallback_requests[0].headers["authorization"] == f"Bearer {TEST_TOKEN}"

    docs = store.list_documents({"acme-waf"})
    assert len(docs) == 1
    assert docs[0].adapter == SourceAdapter.EARNINGS_WATCH
    assert docs[0].external_id == "asset-waf-1"
    assert docs[0].title == "Q3 2026 Results Release"
    sections = store.get_sections(docs[0].document_id)
    assert any("Revenue increased 14 percent" in s.markdown for s in sections)
    store.close()


@pytest.mark.asyncio
async def test_sync_once_does_not_fallback_for_non_earnings_watch_or_without_ir_asset_id(
    tmp_path: Path,
) -> None:
    store = FilingsStore(tmp_path / "filings.sqlite3")
    cas = ContentAddressedStore(tmp_path / "objects")

    payload = LibraryTargetsPayload(
        schema=2,
        generated_at="2026-11-28T00:00:00Z",
        companies=[
            CompanyTarget(
                source_key="acme-nofallback",
                ticker="ACNF",
                exchange="XASX",
                name="Acme NoFallback Ltd",
                verified_documents=[
                    VerifiedDocumentHint(
                        id="vdoc-no-asset-id",
                        url="https://ir.acme.example/verified-only.html",
                        title="Verified Only Release",
                        role="results_release",
                        category="results_release",
                        published_date="2026-11-20",
                    )
                ],
                ir_assets=[
                    VerifiedDocumentHint(
                        id="asset-404-only",
                        url="https://ir.acme.example/missing-404.html",
                        title="Missing 404 Release",
                        role="results_release",
                        category="results_release",
                        published_date="2026-11-20",
                    )
                ],
            )
        ],
        scopes=[{"person": "user-1", "source_keys": ["acme-nofallback"]}],
    )

    fallback_called: list[str] = []

    class _TrackingTargetsClient(LibraryTargetsClient):
        async def fetch_targets(self, *, force_refresh: bool = False) -> LibraryTargetsPayload:
            del force_refresh
            return payload

        async def download_ir_asset(
            self, asset_id: str, *, max_bytes: int = 250 * 1024 * 1024
        ) -> tuple[bytes, str]:
            del max_bytes
            fallback_called.append(asset_id)
            return b"<html><body>should not be called</body></html>", "text/html"

    class _FakeAsxAdapter(AsxAdapter):
        async def discover(
            self, company: CompanyTarget, *, today: date | None = None
        ) -> list[FilingCandidate]:
            del today
            return [
                FilingCandidate(
                    source_key=company.source_key,
                    adapter=SourceAdapter.ASX,
                    external_id="asx-doc-1",
                    form_type="annual_report",
                    form_family=FormFamily.ANNUAL_REPORT,
                    title="ASX 403 Filing",
                    filing_date="2026-11-19",
                    source_url="https://announcements.asx.com.au/asxpdf/20261119/pdf/test.pdf",
                    source_mime="application/pdf",
                    ir_asset_id="spoofed-asset-id",
                )
            ]

    def direct_download_handler(request: httpx.Request) -> httpx.Response:
        if "missing-404.html" in str(request.url):
            return httpx.Response(404, text="Not Found")
        return httpx.Response(403, text="Forbidden")

    targets_client = _TrackingTargetsClient(base_url="http://targets.local", token=TEST_TOKEN)

    res = await sync_once(
        store=store,
        cas=cas,
        targets_client=targets_client,
        extractor_client=ExtractorClient.in_process(),
        config=SyncConfig(),
        asx_adapter=_FakeAsxAdapter(resolver=_public_resolver),
        download_transport=httpx.MockTransport(direct_download_handler),
        url_resolver=_public_resolver,
        today=date(2026, 11, 28),
    )

    assert fallback_called == []
    assert res.documents_ingested == 0
    assert len(res.errors) == 3
    assert any("verified-only.html" in err and "403" in err for err in res.errors)
    assert any("announcements.asx.com.au" in err and "403" in err for err in res.errors)
    assert any("missing-404.html" in err and "404" in err for err in res.errors)
    store.close()


@pytest.mark.asyncio
async def test_sync_once_records_error_and_continues_when_fallback_also_fails(
    tmp_path: Path,
) -> None:
    store = FilingsStore(tmp_path / "filings.sqlite3")
    cas = ContentAddressedStore(tmp_path / "objects")

    targets_payload = {
        "schema": 2,
        "generated_at": "2026-11-28T00:00:00Z",
        "companies": [
            {
                "source_key": "acme-partial",
                "ticker": "ACPT",
                "exchange": "XNYS",
                "name": "Acme Partial Corp",
                "ir_assets": [
                    {
                        "id": "asset-fail-1",
                        "url": "https://ir.acme.example/fail.html",
                        "title": "Blocked Filing",
                        "role": "results_release",
                        "category": "results_release",
                        "published_date": "2026-11-20",
                    },
                    {
                        "id": "asset-ok-2",
                        "url": "https://ir.acme.example/ok.html",
                        "title": "Accessible Filing",
                        "role": "results_release",
                        "category": "results_release",
                        "published_date": "2026-11-21",
                    },
                ],
            }
        ],
        "scopes": [{"person": "user-1", "source_keys": ["acme-partial"]}],
    }

    def targets_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/library-targets":
            return httpx.Response(200, json=targets_payload)
        if request.url.path == "/internal/ir-assets/asset-fail-1/download":
            return httpx.Response(502, text="Upstream failure")
        return httpx.Response(404)

    def direct_download_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://ir.acme.example/fail.html":
            return httpx.Response(403, text="WAF")
        if str(request.url) == "https://ir.acme.example/ok.html":
            return httpx.Response(
                200,
                content=b"<html><body><h1>Accessible</h1><p>OK body</p></body></html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    targets_client = LibraryTargetsClient(
        base_url="http://targets.local",
        token=TEST_TOKEN,
        transport=httpx.MockTransport(targets_handler),
    )

    res = await sync_once(
        store=store,
        cas=cas,
        targets_client=targets_client,
        extractor_client=ExtractorClient.in_process(),
        config=SyncConfig(),
        download_transport=httpx.MockTransport(direct_download_handler),
        url_resolver=_public_resolver,
        today=date(2026, 11, 28),
    )

    assert res.documents_ingested == 1
    assert len(res.errors) == 1
    assert "asset-fail-1" in res.errors[0]
    assert "502" in res.errors[0]
    store.close()


@pytest.mark.asyncio
async def test_targets_client_download_ir_asset_bearer_auth_max_bytes_and_no_redirects() -> None:
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.url.path == "/internal/ir-assets/asset-ok/download":
            return httpx.Response(
                200,
                content=b"%PDF-1.7 synthetic bytes",
                headers={"content-type": "application/pdf; charset=binary"},
            )
        if request.url.path == "/internal/ir-assets/asset-declared-big/download":
            return httpx.Response(
                200,
                content=b"x" * 20,
                headers={"content-type": "application/pdf", "content-length": "9999"},
            )
        if request.url.path == "/internal/ir-assets/asset-stream-big/download":
            return httpx.Response(
                200,
                content=b"y" * 256,
                headers={"content-type": "application/pdf"},
            )
        if request.url.path == "/internal/ir-assets/asset-redirect/download":
            return httpx.Response(
                302,
                headers={"location": "https://evil.example/steal-token"},
            )
        return httpx.Response(404)

    client = LibraryTargetsClient(
        base_url="http://targets.local",
        token=TEST_TOKEN,
        transport=httpx.MockTransport(handler),
    )

    raw_bytes, content_type = await client.download_ir_asset("asset-ok", max_bytes=1024)
    assert raw_bytes == b"%PDF-1.7 synthetic bytes"
    assert content_type == "application/pdf"
    assert seen_requests[0].url.path == "/internal/ir-assets/asset-ok/download"
    assert seen_requests[0].headers["authorization"] == f"Bearer {TEST_TOKEN}"

    with pytest.raises(ValueError, match="Content-Length"):
        await client.download_ir_asset("asset-declared-big", max_bytes=100)

    with pytest.raises(ValueError, match="max_bytes"):
        await client.download_ir_asset("asset-stream-big", max_bytes=100)

    with pytest.raises(HubUnavailableError, match="302"):
        await client.download_ir_asset("asset-redirect", max_bytes=1024)

    with pytest.raises(ValueError, match="Invalid IR asset_id"):
        await client.download_ir_asset("../escape", max_bytes=1024)
