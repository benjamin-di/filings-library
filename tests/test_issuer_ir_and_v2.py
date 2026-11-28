"""Tests for v2 targets compatibility, transcript/statements roles, and retired ir_gap_backfill removal."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx

from filings_library.adapters.earnings_watch import EarningsWatchAdapter
from filings_library.cas import ContentAddressedStore
from filings_library.extractor.app import ExtractorClient
from filings_library.models import (
    CompanyTarget,
    ExtractedDocument,
    ExtractedSection,
    FormFamily,
    LibraryTargetsPayload,
)
from filings_library.store import FilingsStore
from filings_library.targets_client import LibraryTargetsClient
from filings_library.worker import SyncConfig, sync_once


def _public_resolver(host: str, port: int) -> list[str]:
    del host, port
    return ["93.184.216.34"]


async def test_targets_client_accepts_schema_v2_and_typed_ir_urls() -> None:
    v2_payload = {
        "schema": 2,
        "generated_at": "2026-11-27T00:00:00Z",
        "companies": [
            {
                "source_key": "acme-corp",
                "ticker": "ACME",
                "exchange": "XNAS",
                "name": "Acme Corp",
                "fy_end_month": 3,
                "period_scheme": "fiscal-q",
                "ir_urls": {
                    "calendar": "https://investor.acme.example/events",
                    "presentations": "https://investor.acme.example/presentations",
                    "transcripts": "https://investor.acme.example/transcripts",
                },
                "verified_documents": [
                    {
                        "id": "doc-1",
                        "event_id": "earnings:ACME:2026FQ3",
                        "url": "https://s2.q4cdn.com/99/q3-transcript.pdf",
                        "title": "Q3 2026 Earnings Call Transcript",
                        "role": "transcript",
                        "category": "transcript",
                        "first_seen": "2026-11-20T12:00:00Z",
                    },
                    {
                        "id": "doc-2",
                        "event_id": "earnings:ACME:2026FQ3",
                        "url": "https://s2.q4cdn.com/99/q3-statements.pdf",
                        "title": "Q3 2026 Financial Statements",
                        "role": "statements",
                        "first_seen": "2026-11-20T12:00:00Z",
                    },
                ],
            }
        ],
        "scopes": [{"person": "user-1", "source_keys": ["acme-corp"]}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=v2_payload)

    client = LibraryTargetsClient(
        base_url="http://targets.local",
        token="t" * 40,
        transport=httpx.MockTransport(handler),
    )
    payload = await client.fetch_targets()
    assert payload.schema_version == 2
    company = payload.companies[0]
    assert company.ir_urls == [
        "https://investor.acme.example/events",
        "https://investor.acme.example/presentations",
        "https://investor.acme.example/transcripts",
    ]

    adapter = EarningsWatchAdapter(resolver=_public_resolver)
    cands = await adapter.discover(company)
    by_id = {c.external_id: c for c in cands}
    assert by_id["doc-1"].form_family == FormFamily.TRANSCRIPT
    assert by_id["doc-2"].form_family == FormFamily.FINANCIAL_STATEMENTS


async def test_sync_once_does_not_enqueue_retired_ir_gap_backfill_job(
    tmp_path: Path,
) -> None:
    store = FilingsStore(tmp_path / "filings.db")
    cas = ContentAddressedStore(tmp_path / "objects")

    payload = LibraryTargetsPayload(
        schema=2,
        generated_at="2026-11-27T00:00:00Z",
        companies=[
            CompanyTarget(
                source_key="gap-issuer",
                ticker="GAP",
                exchange="XTKS",
                name="Gap Corp",
                ir_urls=[],
                verified_documents=[],
            )
        ],
        scopes=[],
    )

    class _StaticTargets(LibraryTargetsClient):
        async def fetch_targets(self, *, force_refresh: bool = False) -> LibraryTargetsPayload:
            del force_refresh
            return payload

    class _NoopExtractor(ExtractorClient):
        async def extract(
            self, raw_bytes: bytes, *, mime_type: str, filename: str
        ) -> ExtractedDocument:
            del raw_bytes, mime_type, filename
            return ExtractedDocument(
                converter_id="test",
                converter_version="1",
                locator_kind="page",
                markdown="",
                sections=[ExtractedSection(locator="page:1", ordinal=1, markdown="")],
            )

    targets = _StaticTargets(base_url="http://targets.local", token="t" * 40)
    extractor = _NoopExtractor(base_url="http://extractor.local")

    await sync_once(
        store=store,
        cas=cas,
        targets_client=targets,
        extractor_client=extractor,
        config=SyncConfig(),
        url_resolver=_public_resolver,
        today=date(2026, 11, 27),
    )
    assert store.lease_job("researcher-1") is None
    store.close()
