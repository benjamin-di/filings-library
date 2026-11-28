"""ESEF / EU & UK annual iXBRL adapter via filings.xbrl.org."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

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

_KNOWN_ESEF_LANGUAGES = frozenset(
    {
        "en",
        "de",
        "it",
        "fr",
        "pt",
        "es",
        "nl",
        "sv",
        "da",
        "fi",
        "no",
        "nb",
        "pl",
        "cs",
        "hu",
        "ro",
        "el",
        "bg",
        "hr",
        "sk",
        "sl",
        "et",
        "lv",
        "lt",
    }
)
_URL_LANG_SUFFIX_RE = re.compile(r"[-_]([a-z]{2})(?:[-_][a-z]{2})?\.(?:x?html?)$", re.IGNORECASE)
_URL_LANG_SEGMENT_RE = re.compile(r"/([a-z]{2})/", re.IGNORECASE)
_FXO_REV_RE = re.compile(r"/(\d+)$")


def _detect_esef_language(report_url: str, attrs: dict[str, Any]) -> str:
    """Detect ISO-639-1 language from ESEF attributes or report URL/filename."""
    for key in ("language", "lang"):
        raw = str(attrs.get(key) or "").strip().lower()
        if raw:
            code = raw.split("-", 1)[0].split("_", 1)[0]
            if code in _KNOWN_ESEF_LANGUAGES:
                return code

    path = urlparse(report_url).path
    suffix_match = _URL_LANG_SUFFIX_RE.search(path)
    if suffix_match:
        code = suffix_match.group(1).lower()
        if code in _KNOWN_ESEF_LANGUAGES:
            return code

    for seg_match in _URL_LANG_SEGMENT_RE.finditer(path):
        code = seg_match.group(1).lower()
        if code in _KNOWN_ESEF_LANGUAGES:
            return code

    return "en"


def _parse_esef_revision(external_id: str) -> int:
    match = _FXO_REV_RE.search(external_id or "")
    return int(match.group(1)) if match else -1


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

        best_by_key: dict[tuple[str, str], tuple[tuple[str, int, str], FilingCandidate]] = {}
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
            raw_timestamp = (
                str(attrs.get("date_added") or "").strip()
                or str(attrs.get("processed") or "").strip()
                or (period_end or "")
            )
            filing_date = _extract_iso_date(raw_timestamp)
            if not filing_date or filing_date < cutoff:
                continue

            external_id = str(attrs.get("fxo_id") or item.get("id") or report_url).strip()
            country = str(attrs.get("country") or "").strip().upper()
            language = _detect_esef_language(report_url, attrs)
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

            candidate = FilingCandidate(
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
                language=language,
                refetchable=True,
                is_amendment=False,
            )
            dedup_key = (period_end or filing_date, language)
            rank = (raw_timestamp, _parse_esef_revision(external_id), external_id)
            prev = best_by_key.get(dedup_key)
            if prev is None or rank > prev[0]:
                best_by_key[dedup_key] = (rank, candidate)

        return [pair[1] for pair in best_by_key.values()]
