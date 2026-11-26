"""Locked data contracts shared across store, extractor, adapters, MCP, and CLI."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FormFamily(StrEnum):
    ANNUAL_REPORT = "annual_report"
    INTERIM_REPORT = "interim_report"
    QUARTERLY_REPORT = "quarterly_report"
    RESULTS_RELEASE = "results_release"
    CURRENT_REPORT = "current_report"
    PROXY = "proxy"
    OFFERING = "offering"
    CIRCULAR = "circular"
    PRESENTATION = "presentation"
    DATABOOK = "databook"
    OTHER = "other"


class ArtifactKind(StrEnum):
    RAW = "raw"
    OCR_PDF = "ocr_pdf"
    MARKDOWN = "markdown"
    FACTS_JSON = "facts_json"


class SourceAdapter(StrEnum):
    SEC_EDGAR = "sec_edgar"
    HKEXNEWS = "hkexnews"
    ESEF_XBRL = "esef_xbrl"
    EARNINGS_WATCH = "earnings_watch"


class VerifiedDocumentHint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    event_id: str
    url: str
    title: str = ""
    role: str = "other"
    first_seen: str = ""


class CompanyTarget(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source_key: str
    ticker: str
    exchange: str = ""
    name: str
    isin: str | None = None
    sec_cik: int | None = None
    hkex_stock_id: str | None = None
    lei: str | None = None
    ir_urls: list[str] = Field(default_factory=list)
    verified_documents: list[VerifiedDocumentHint] = Field(default_factory=list)


class PersonScope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    person: str
    source_keys: list[str] = Field(default_factory=list)


class LibraryTargetsPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: int = Field(alias="schema", default=1)
    generated_at: str
    companies: list[CompanyTarget] = Field(default_factory=list)
    scopes: list[PersonScope] = Field(default_factory=list)


class ExtractedSection(BaseModel):
    """Unified locator unit: `page:N` for PDFs, `section:<slug>` or `item:<id>` for HTML/iXBRL."""

    model_config = ConfigDict(extra="forbid")
    locator: str
    title: str = ""
    ordinal: int
    markdown: str
    ocr_used: bool = False


class ExtractedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    converter_id: str
    converter_version: str
    locator_kind: Literal["page", "section"]
    markdown: str
    sections: list[ExtractedSection] = Field(default_factory=list)
    page_count: int | None = None
    ocr_pages: list[int] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DocumentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    source_key: str
    adapter: SourceAdapter
    external_id: str
    form_type: str
    form_family: FormFamily
    title: str
    filing_date: str
    period_end: str | None = None
    language: str = "en"
    source_url: str
    source_sha256: str
    source_mime: str
    source_bytes: int
    refetchable: bool = True
    is_amendment: bool = False
    supersedes_document_id: str | None = None
    converter_id: str | None = None
    converter_version: str | None = None
    markdown_sha256: str | None = None
    page_count: int | None = None


class Citation(BaseModel):
    """Strict citation contract returned by every search hit and read call."""

    model_config = ConfigDict(extra="forbid")
    document_id: str
    source_key: str
    form_type: str
    period_end: str | None = None
    filing_date: str
    source_sha256: str
    locator: str
    source_url: str
    converter_id: str
    snippet: str = ""
    score: float = 0.0
