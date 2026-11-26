"""Structured source adapters for SEC EDGAR, HKEXnews, ESEF XBRL, and Earnings Watch."""

from __future__ import annotations

from dataclasses import dataclass

from filings_library.models import FormFamily, SourceAdapter


@dataclass(frozen=True)
class FilingCandidate:
    """Normalized candidate filing discovered by a source adapter prior to download."""

    source_key: str
    adapter: SourceAdapter
    external_id: str
    form_type: str
    form_family: FormFamily
    title: str
    filing_date: str
    source_url: str
    source_mime: str
    period_end: str | None = None
    language: str = "en"
    refetchable: bool = True
    is_amendment: bool = False
    base_form_type: str | None = None
