"""Earnings Watch verified-document fast-path adapter (`refetchable=False`)."""

from __future__ import annotations

from datetime import date

from filings_library.adapters import FilingCandidate
from filings_library.models import CompanyTarget, FormFamily, SourceAdapter

_ROLE_TO_FAMILY: dict[str, FormFamily] = {
    "results_release": FormFamily.RESULTS_RELEASE,
    "earnings_release": FormFamily.RESULTS_RELEASE,
    "press_release": FormFamily.RESULTS_RELEASE,
    "presentation": FormFamily.PRESENTATION,
    "slides": FormFamily.PRESENTATION,
    "databook": FormFamily.DATABOOK,
    "spreadsheet": FormFamily.DATABOOK,
    "annual_report": FormFamily.ANNUAL_REPORT,
    "interim_report": FormFamily.INTERIM_REPORT,
    "quarterly_report": FormFamily.QUARTERLY_REPORT,
}


def _guess_mime(url: str) -> str:
    lower = url.split("?", 1)[0].lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return "text/html"


class EarningsWatchAdapter:
    """Ingests high-priority release-day `verified_documents` from `CompanyTarget`."""

    async def discover(
        self, company: CompanyTarget, *, today: date | None = None
    ) -> list[FilingCandidate]:
        ref_date = (today or date.today()).isoformat()
        candidates: list[FilingCandidate] = []
        for hint in company.verified_documents:
            if not hint.url.strip():
                continue
            role_key = (hint.role or "other").strip().lower()
            form_family = _ROLE_TO_FAMILY.get(role_key, FormFamily.RESULTS_RELEASE)
            fdate = hint.first_seen[:10] if len(hint.first_seen) >= 10 else ref_date
            candidates.append(
                FilingCandidate(
                    source_key=company.source_key,
                    adapter=SourceAdapter.EARNINGS_WATCH,
                    external_id=hint.id,
                    form_type=hint.role or "results_release",
                    form_family=form_family,
                    title=hint.title or f"{company.name} {hint.role}",
                    filing_date=fdate,
                    period_end=None,
                    source_url=hint.url,
                    source_mime=_guess_mime(hint.url),
                    language="en",
                    refetchable=False,
                    is_amendment=False,
                )
            )
        return candidates
