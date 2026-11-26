"""Stateless document extraction pipelines (HTML/iXBRL, PDF, XLSX) and HTTP/ASGI app."""

from filings_library.extractor.app import ExtractorClient, create_extractor_app
from filings_library.extractor.html_ixbrl import extract_html_ixbrl
from filings_library.extractor.pdf_extract import EncryptedPdfError, extract_pdf
from filings_library.extractor.xlsx_extract import extract_xlsx

__all__ = [
    "EncryptedPdfError",
    "ExtractorClient",
    "create_extractor_app",
    "extract_html_ixbrl",
    "extract_pdf",
    "extract_xlsx",
]
