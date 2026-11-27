"""Deterministic, zero-LLM issuer IR page link harvester (presentations, transcripts, databooks)."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from urllib.parse import urlsplit

import httpx
from issuer_sources.model import DocumentCategory, IrUrls, IssuerIdentity, PeriodScheme
from issuer_sources.transport import HostPolicy, SafeTransport
from issuer_sources.webpage import capture_html, verify_document_link

from filings_library.adapters import FilingCandidate
from filings_library.models import CompanyTarget, FormFamily, SourceAdapter
from filings_library.url_safety import HostResolver

_ASSET_EXT_RE = re.compile(r"\.(?:pdf|xlsx)$", re.I)

_HARVEST_CATEGORIES: tuple[tuple[DocumentCategory, FormFamily], ...] = (
    (DocumentCategory.PRESENTATION, FormFamily.PRESENTATION),
    (DocumentCategory.TRANSCRIPT, FormFamily.TRANSCRIPT),
    (DocumentCategory.DATABOOK, FormFamily.DATABOOK),
    (DocumentCategory.ANNUAL_REPORT, FormFamily.ANNUAL_REPORT),
    (DocumentCategory.INTERIM_REPORT, FormFamily.INTERIM_REPORT),
    (DocumentCategory.RESULTS_RELEASE, FormFamily.RESULTS_RELEASE),
)


def _guess_mime(url: str) -> str:
    lower = url.split("?", 1)[0].lower()
    if lower.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if lower.endswith(".pdf"):
        return "application/pdf"
    return "text/html"


class IssuerIrAdapter:
    """Deterministically harvests verified PDF/XLSX links from an issuer's official IR pages."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: HostResolver | None = None,
        max_pages_per_issuer: int = 4,
    ) -> None:
        self._transport = transport
        self._resolver = resolver
        self._max_pages_per_issuer = max(1, max_pages_per_issuer)

    async def discover(
        self, company: CompanyTarget, *, today: date | None = None
    ) -> list[FilingCandidate]:
        if not company.ir_urls:
            return []

        ref_date = (today or date.today()).isoformat()
        ir_urls = IrUrls(extra=list(company.ir_urls))
        try:
            scheme = PeriodScheme(company.period_scheme)
        except ValueError:
            scheme = PeriodScheme.CALENDAR_Q

        issuer = IssuerIdentity(
            source_key=company.source_key,
            ticker=company.ticker,
            exchange=company.exchange,
            name=company.name,
            isin=company.isin,
            sec_cik=company.sec_cik,
            hkex_stock_id=company.hkex_stock_id,
            lei=company.lei,
            fy_end_month=company.fy_end_month,
            period_scheme=scheme,
            ir_urls=ir_urls,
        )
        safe_transport = SafeTransport(
            hosts=ir_urls.hosts(),
            apex_domains=ir_urls.apex_domains() or None,
            default_policy=HostPolicy(daily_budget=30, pace_seconds=0.0),
            mock_transport=self._transport,
            resolver=self._resolver,
        )

        candidates: list[FilingCandidate] = []
        seen_urls: set[str] = set()
        for page_url in ir_urls.all_urls()[: self._max_pages_per_issuer]:
            try:
                capture = await capture_html(safe_transport, page_url, fetch_same_host_ics=False)
            except Exception:
                continue
            for link in capture.links:
                path = urlsplit(link.url).path
                if not _ASSET_EXT_RE.search(path) or not link.title:
                    continue
                if link.url in seen_urls:
                    continue
                for category, family in _HARVEST_CATEGORIES:
                    check = verify_document_link(
                        issuer,
                        capture,
                        url=link.url,
                        title=link.title,
                        evidence=link.title,
                        category=category,
                    )
                    if check.verified:
                        seen_urls.add(link.url)
                        ext_id = hashlib.sha256(link.url.encode("utf-8")).hexdigest()[:24]
                        candidates.append(
                            FilingCandidate(
                                source_key=company.source_key,
                                adapter=SourceAdapter.ISSUER_IR,
                                external_id=ext_id,
                                form_type=category.value,
                                form_family=family,
                                title=check.document.title or f"{company.name} {category.value}",
                                filing_date=ref_date,
                                period_end=None,
                                source_url=check.document.url,
                                source_mime=_guess_mime(check.document.url),
                                language="en",
                                refetchable=False,
                                is_amendment=False,
                            )
                        )
                        break
        return candidates
