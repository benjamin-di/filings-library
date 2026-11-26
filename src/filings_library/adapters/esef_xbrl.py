"""ESEF / EU & UK annual iXBRL adapter via filings.xbrl.org."""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from filings_library.adapters import FilingCandidate
from filings_library.models import CompanyTarget, FormFamily, SourceAdapter
from filings_library.url_safety import (
    ESEF_ALLOWED_HOSTS,
    HostResolver,
    UnsafeUrlError,
    validate_public_https_url,
)

ESEF_API_BASE = "https://filings.xbrl.org"
ESEF_FILINGS_URL = f"{ESEF_API_BASE}/api/filings"


def _resolve_url(raw: str) -> str:
    if raw.startswith(("http://", "https://")):
        return raw
    return f"{ESEF_API_BASE}{raw if raw.startswith('/') else '/' + raw}"


def _extract_iso_date(raw: str | None) -> str:
    if not raw:
        return ""
    return str(raw).strip()[:10]


class EsefXbrlAdapter:
    """Queries `filings.xbrl.org` by LEI for EU/UK annual iXBRL reports."""

    def __init__(
        self,
        *,
        backfill_years: int = 3,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: HostResolver | None = None,
    ) -> None:
        self._backfill_years = max(1, backfill_years)
        self._timeout = timeout
        self._transport = transport
        self._resolver = resolver

    async def discover(
        self, company: CompanyTarget, *, today: date | None = None
    ) -> list[FilingCandidate]:
        lei = (company.lei or "").strip()
        if not lei:
            return []

        ref_date = today or date.today()
        cutoff = (ref_date - timedelta(days=365 * self._backfill_years)).isoformat()

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            resp = await client.get(
                ESEF_FILINGS_URL,
                params={"filter[entity.identifier]": lei},
                headers={"Accept": "application/vnd.api+json, application/json"},
            )
            resp.raise_for_status()
            payload = resp.json()

        items = payload.get("data", [])
        if not isinstance(items, list):
            return []

        candidates: list[FilingCandidate] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes") or {}
            if not isinstance(attrs, dict):
                continue

            report_url = str(attrs.get("report_url") or "").strip()
            if not report_url:
                continue
            lower_url = report_url.lower()
            if not lower_url.endswith((".html", ".htm", ".xhtml")):
                continue

            period_end = _extract_iso_date(attrs.get("period_end")) or None
            filing_date = (
                _extract_iso_date(attrs.get("date_added"))
                or _extract_iso_date(attrs.get("processed"))
                or period_end
                or ""
            )
            if not filing_date or filing_date < cutoff:
                continue

            external_id = str(attrs.get("fxo_id") or item.get("id") or report_url).strip()
            country = str(attrs.get("country") or "").strip().upper()
            title = f"{company.name} ESEF Annual Report"
            if period_end:
                title = f"{title} ({period_end})"
            if country:
                title = f"{title} [{country}]"

            try:
                resolved_url = validate_public_https_url(
                    _resolve_url(report_url),
                    allowed_hosts=ESEF_ALLOWED_HOSTS,
                    resolver=self._resolver,
                )
            except UnsafeUrlError:
                continue

            candidates.append(
                FilingCandidate(
                    source_key=company.source_key,
                    adapter=SourceAdapter.ESEF_XBRL,
                    external_id=external_id,
                    form_type="ESEF-AR",
                    form_family=FormFamily.ANNUAL_REPORT,
                    title=title,
                    filing_date=filing_date,
                    period_end=period_end,
                    source_url=resolved_url,
                    source_mime="application/xhtml+xml"
                    if lower_url.endswith(".xhtml")
                    else "text/html",
                    language="en",
                    refetchable=True,
                    is_amendment=False,
                )
            )

        return candidates
