"""ASX announcements adapter via MarkitDigital JSON API with ASX HTML fallback."""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import httpx

from filings_library.adapters import FilingCandidate
from filings_library.models import CompanyTarget, FormFamily, SourceAdapter
from filings_library.url_safety import (
    ASX_ALLOWED_HOSTS,
    HostResolver,
    UnsafeUrlError,
    validate_public_https_url,
)

ASX_MARKIT_URL = "https://asx.api.markitdigital.com/asx-research/1.0/companies/{code}/announcements"
ASX_MARKIT_FILE_URL = (
    "https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/file/{document_key}"
)
ASX_ANNUAL_HTML_URL = "https://www.asx.com.au/asx/v2/statistics/announcements.do"
ASX_WEB_BASE = "https://www.asx.com.au"

_ASX_CODE_RE = re.compile(r"^[A-Z0-9]{1,6}$")
_ASX_DOC_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ASX_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_ASX_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_ASX_LINK_RE = re.compile(
    r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

_REJECTED_ASX_SUBSTRINGS = (
    "appendix 3y",
    "appendix 3x",
    "appendix 3z",
    "appendix 3b",
    "appendix 3c",
    "appendix 3e",
    "appendix 3f",
    "appendix 3g",
    "appendix 3h",
    "appendix 2a",
    "change of director",
    "initial director",
    "final director",
    "cleansing notice",
    "daily share buy-back",
    "buy-back notification",
    "update - notification of buy-back",
    "notification regarding unquoted",
    "application for quotation",
    "notification of cessation",
    "becoming a substantial holder",
    "ceasing to be a substantial holder",
    "change in substantial holding",
    "dividend/distribution",
    "issue of securities",
)


def _strip_html(raw: str) -> str:
    no_tags = _HTML_TAG_RE.sub(" ", raw or "")
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def classify_asx_announcement(header: str) -> tuple[bool, FormFamily, str]:
    """Classify an ASX announcement headline into `(kept, form_family, form_type)`."""
    cleaned = _strip_html(header)
    lower = cleaned.lower()
    if not lower:
        return False, FormFamily.OTHER, ""
    if any(noise in lower for noise in _REJECTED_ASX_SUBSTRINGS):
        return False, FormFamily.OTHER, ""

    if "appendix 4e" in lower:
        return True, FormFamily.ANNUAL_REPORT, "Appendix 4E"
    if "annual report" in lower or "full year statutory accounts" in lower:
        return True, FormFamily.ANNUAL_REPORT, "Annual Report"

    if "appendix 4d" in lower:
        return True, FormFamily.INTERIM_REPORT, "Appendix 4D"
    if (
        "half-year report" in lower
        or "half year report" in lower
        or "half-year financial" in lower
        or "half year financial" in lower
        or "interim report" in lower
        or "interim financial" in lower
    ):
        return True, FormFamily.INTERIM_REPORT, "Half-Year Report"

    if "appendix 4c" in lower or "appendix 5b" in lower:
        return True, FormFamily.QUARTERLY_REPORT, "Quarterly Report"
    if "quarterly" in lower and any(
        k in lower for k in ("report", "results", "activities", "update")
    ):
        return True, FormFamily.QUARTERLY_REPORT, "Quarterly Report"

    if "presentation" in lower and any(
        k in lower
        for k in (
            "investor",
            "results",
            "earnings",
            "full year",
            "half year",
            "half-year",
            "fy",
            "hy",
            "1h",
            "2h",
            "agm",
            "strategy",
        )
    ):
        return True, FormFamily.PRESENTATION, "Investor Presentation"

    if any(
        k in lower
        for k in (
            "full year results",
            "full-year results",
            "annual results",
            "half year results",
            "half-year results",
            "interim results",
            "financial results",
            "results announcement",
            "earnings release",
        )
    ):
        return True, FormFamily.RESULTS_RELEASE, "Results Announcement"

    return False, FormFamily.OTHER, ""


def _parse_asx_iso_date(raw: str | None) -> str:
    if not raw:
        return ""
    text = str(raw).strip()
    if len(text) >= 10:
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            pass
    match = _ASX_DATE_RE.search(text)
    if match:
        day, month, year = match.group(1), match.group(2), match.group(3)
        try:
            return datetime(int(year), int(month), int(day)).date().isoformat()
        except ValueError:
            return ""
    return ""


def _extract_asx_code(company: CompanyTarget) -> str | None:
    is_asx = company.exchange.upper() in {"XASX", "ASX"} or company.ticker.upper().endswith(".AX")
    if not is_asx:
        return None
    code = company.ticker.split(".", 1)[0].strip().upper()
    if not _ASX_CODE_RE.match(code):
        return None
    return code


class AsxAdapter:
    """SSRF-safe ASX adapter using MarkitDigital JSON API + ASX annual HTML fallback."""

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
        code = _extract_asx_code(company)
        if not code:
            return []

        ref_date = today or date.today()
        cutoff = (ref_date - timedelta(days=365 * self._backfill_years)).isoformat()

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            candidates = await self._discover_markit(client, company, code, cutoff=cutoff)
            if candidates is not None:
                return candidates
            return await self._discover_html_fallback(
                client, company, code, ref_date=ref_date, cutoff=cutoff
            )

    async def _discover_markit(
        self,
        client: httpx.AsyncClient,
        company: CompanyTarget,
        code: str,
        *,
        cutoff: str,
    ) -> list[FilingCandidate] | None:
        api_url = validate_public_https_url(
            ASX_MARKIT_URL.format(code=code),
            allowed_hosts=ASX_ALLOWED_HOSTS,
            resolver=self._resolver,
        )
        try:
            resp = await client.get(
                api_url,
                params={"count": "100", "market_sensitive": "false"},
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
        except Exception:
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        items: Any = (
            data.get("items")
            if isinstance(data, dict)
            else (payload.get("items") if isinstance(payload, dict) else None)
        )
        if not isinstance(items, list):
            return None

        candidates: list[FilingCandidate] = []
        seen_ids: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            header = _strip_html(str(item.get("header") or item.get("headline") or ""))
            kept, form_family, form_type = classify_asx_announcement(header)
            if not kept:
                continue

            fdate = _parse_asx_iso_date(
                str(item.get("documentReleaseDate") or item.get("releaseDate") or "")
            )
            if not fdate or fdate < cutoff:
                continue

            doc_key = str(item.get("documentKey") or "").strip()
            raw_url = str(item.get("url") or "").strip()
            if not raw_url and doc_key:
                if not _ASX_DOC_KEY_RE.fullmatch(doc_key) or doc_key in {".", ".."}:
                    continue
                raw_url = ASX_MARKIT_FILE_URL.format(document_key=doc_key)
            if not raw_url:
                continue
            if raw_url.startswith("/"):
                raw_url = urljoin(ASX_WEB_BASE, raw_url)

            try:
                safe_url = validate_public_https_url(
                    raw_url,
                    allowed_hosts=ASX_ALLOWED_HOSTS,
                    resolver=self._resolver,
                )
            except UnsafeUrlError:
                continue

            ext_id = doc_key or safe_url
            if ext_id in seen_ids:
                continue
            seen_ids.add(ext_id)

            candidates.append(
                FilingCandidate(
                    source_key=company.source_key,
                    adapter=SourceAdapter.ASX,
                    external_id=ext_id,
                    form_type=form_type,
                    form_family=form_family,
                    title=header,
                    filing_date=fdate,
                    period_end=None,
                    source_url=safe_url,
                    source_mime="application/pdf",
                    language="en",
                    refetchable=True,
                    is_amendment=False,
                )
            )
        return candidates

    async def _discover_html_fallback(
        self,
        client: httpx.AsyncClient,
        company: CompanyTarget,
        code: str,
        *,
        ref_date: date,
        cutoff: str,
    ) -> list[FilingCandidate]:
        base_url = validate_public_https_url(
            ASX_ANNUAL_HTML_URL,
            allowed_hosts=ASX_ALLOWED_HOSTS,
            resolver=self._resolver,
        )
        start_year = max(ref_date.year - self._backfill_years, 2000)
        candidates: list[FilingCandidate] = []
        seen_urls: set[str] = set()

        for year in range(ref_date.year, start_year - 1, -1):
            resp = await client.get(
                base_url,
                params={
                    "by": "asxCode",
                    "asxCode": code,
                    "timeframe": "Y",
                    "year": str(year),
                },
            )
            if resp.status_code != 200:
                continue
            body = resp.text
            for row_match in _ASX_ROW_RE.finditer(body):
                row_html = row_match.group(1)
                fdate = _parse_asx_iso_date(row_html)
                if not fdate or fdate < cutoff:
                    continue
                link_match = _ASX_LINK_RE.search(row_html)
                if not link_match:
                    continue
                href = html.unescape(link_match.group(1).strip())
                header = _strip_html(link_match.group(2))
                kept, form_family, form_type = classify_asx_announcement(header)
                if not kept:
                    continue

                raw_url = urljoin(ASX_WEB_BASE, href)
                try:
                    safe_url = validate_public_https_url(
                        raw_url,
                        allowed_hosts=ASX_ALLOWED_HOSTS,
                        resolver=self._resolver,
                    )
                except UnsafeUrlError:
                    continue

                if safe_url in seen_urls:
                    continue
                seen_urls.add(safe_url)

                candidates.append(
                    FilingCandidate(
                        source_key=company.source_key,
                        adapter=SourceAdapter.ASX,
                        external_id=safe_url.rsplit("/", 1)[-1] or safe_url,
                        form_type=form_type,
                        form_family=form_family,
                        title=header,
                        filing_date=fdate,
                        period_end=None,
                        source_url=safe_url,
                        source_mime="application/pdf",
                        language="en",
                        refetchable=True,
                        is_amendment=False,
                    )
                )

        return candidates
