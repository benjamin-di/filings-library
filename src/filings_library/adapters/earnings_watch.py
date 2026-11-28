"""Earnings Watch verified-document fast-path adapter (`refetchable=False`)."""

from __future__ import annotations

import re
from datetime import date

from issuer_sources.model import DocumentCategory, normalize_document_category

from filings_library.adapters import FilingCandidate
from filings_library.models import CompanyTarget, FormFamily, SourceAdapter
from filings_library.url_safety import (
    HostResolver,
    UnsafeUrlError,
    validate_public_https_url,
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_CATEGORY_TO_FAMILY: dict[DocumentCategory, FormFamily] = {
    DocumentCategory.RESULTS_RELEASE: FormFamily.RESULTS_RELEASE,
    DocumentCategory.FINANCIAL_STATEMENTS: FormFamily.FINANCIAL_STATEMENTS,
    DocumentCategory.PRESENTATION: FormFamily.PRESENTATION,
    DocumentCategory.TRANSCRIPT: FormFamily.TRANSCRIPT,
    DocumentCategory.DATABOOK: FormFamily.DATABOOK,
    DocumentCategory.ANNUAL_REPORT: FormFamily.ANNUAL_REPORT,
    DocumentCategory.INTERIM_REPORT: FormFamily.INTERIM_REPORT,
    DocumentCategory.QUARTERLY_REPORT: FormFamily.QUARTERLY_REPORT,
    DocumentCategory.CURRENT_REPORT: FormFamily.CURRENT_REPORT,
    DocumentCategory.PROXY: FormFamily.PROXY,
    DocumentCategory.CIRCULAR: FormFamily.CIRCULAR,
    DocumentCategory.OFFERING: FormFamily.OFFERING,
    DocumentCategory.AUDIO_REPLAY: FormFamily.TRANSCRIPT,
    DocumentCategory.WEBCAST_PAGE: FormFamily.WEBCAST,
}


def _guess_mime(url: str) -> str:
    lower = url.split("?", 1)[0].lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".pptx"):
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if lower.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if lower.endswith((".htm", ".html")):
        return "text/html"
    if lower.endswith(".xhtml"):
        return "application/xhtml+xml"
    if lower.endswith(".mp3"):
        return "audio/mpeg"
    if lower.endswith(".m4a"):
        return "audio/mp4"
    if lower.endswith(".mp4"):
        return "video/mp4"
    return ""


def _parse_iso_date(raw: str | None) -> str:
    if not isinstance(raw, str):
        return "undated"
    prefix = raw.strip()[:10]
    if not _ISO_DATE_RE.match(prefix):
        return "undated"
    try:
        return date.fromisoformat(prefix).isoformat()
    except ValueError:
        return "undated"


class EarningsWatchAdapter:
    """Ingests high-priority release-day `verified_documents` from `CompanyTarget`."""

    def __init__(self, *, resolver: HostResolver | None = None) -> None:
        self._resolver = resolver

    async def discover(
        self, company: CompanyTarget, *, today: date | None = None
    ) -> list[FilingCandidate]:
        del today
        candidates: list[FilingCandidate] = []
        seen_urls: set[str] = set()
        for hint in [*company.verified_documents, *company.ir_assets]:
            raw_url = hint.url.strip()
            if not raw_url:
                continue
            try:
                safe_url = validate_public_https_url(raw_url, resolver=self._resolver)
            except UnsafeUrlError:
                continue
            if safe_url in seen_urls:
                continue
            seen_urls.add(safe_url)
            category = normalize_document_category(hint.category or hint.role or "other")
            form_family = _CATEGORY_TO_FAMILY.get(category, FormFamily.RESULTS_RELEASE)
            fdate = _parse_iso_date(hint.published_date or hint.first_seen)
            if category == DocumentCategory.AUDIO_REPLAY:
                form_type = "earnings_call_audio_replay"
            elif category == DocumentCategory.WEBCAST_PAGE:
                form_type = "webcast_replay"
            else:
                form_type = hint.role or category.value or "results_release"
            candidates.append(
                FilingCandidate(
                    source_key=company.source_key,
                    adapter=SourceAdapter.EARNINGS_WATCH,
                    external_id=hint.id,
                    form_type=form_type,
                    form_family=form_family,
                    title=hint.title or f"{company.name} {form_type}",
                    filing_date=fdate,
                    period_end=hint.period,
                    source_url=safe_url,
                    source_mime=_guess_mime(safe_url),
                    language="en",
                    refetchable=False,
                    is_amendment=False,
                )
            )
        return candidates
