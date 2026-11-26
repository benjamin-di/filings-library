# filings-library — repository guide

Public MIT repository (`benjamin-di/filings-library`). Never commit private hosts,
fleet names, real personal records, or secrets; use synthetic fixtures in tests.

## Architecture invariants
- **Single source of truth:** SQLite (WAL) + immutable SHA-256 CAS (`objects/sha256/...`).
  Derivatives (`ocr_pdf`, `markdown`, `facts_json`) live in CAS keyed by
  `(source_sha256, converter_id, converter_version)`. FTS5 is strictly rebuildable.
- **License boundary:** Apache/BSD/MIT-compatible dependencies only. Do not add
  AGPL/GPL dependencies (no PyMuPDF, no MinerU).
- **Extraction isolation:** PDF/OCR/document parsing runs in the stateless
  `filings-extractor` service (PDFium + Poppler + Tesseract + qpdf), never in the
  API serving process.
- **Multi-user boundary:** Shared public filing corpus; every read/search/status
  API call scopes to the authenticated subject's allowed `source_keys` from
  `/internal/library-targets`, failing closed (`503`) if the upstream hub is
  unavailable. Never expose corpus-wide counts or unfollowed issuer state.

## Quality gate
```sh
make check
```
