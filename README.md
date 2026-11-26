# filings-library

Multi-user company financial filings library: immutable SHA-256 archive, isolated
PDF/OCR/HTML/XLSX extraction, Markdown conversion with unified locators, FTS5
financial search, SEC XBRL `companyfacts`, CLI, and a strictly scoped FastMCP server.

## Quick start

```sh
uv sync --locked --extra extractor
make check
```

Run the reference 3-container stack locally:

```sh
docker compose up --build
```

## Architecture

- **Single source of truth:** SQLite (WAL mode) + immutable SHA-256 CAS
  (`objects/sha256/<aa>/<sha256>`). Derivatives (`ocr_pdf`, `markdown`, `facts_json`)
  are stored in CAS keyed by `(source_sha256, converter_id, converter_version)`.
  The FTS5 index (`fts_chunks`) is strictly rebuildable from `sections`.
- **Extraction isolation:** PDF, OCR, HTML/iXBRL, and XLSX parsing run inside the
  stateless `filings-extractor` container (`pypdfium2`, `poppler-utils`, `tesseract-ocr`,
  `qpdf`) with `read_only: true`, `cap_drop: [ALL]`, `/scratch` tmpfs, memory limits,
  and zero credentials or persistent volumes.
- **Multi-user fail-closed scoping:** Every read, search, facts, and status call
  resolves the authenticated caller's allowed `source_keys` from the targets hub
  (`GET /internal/library-targets`, TTL <= 60s). If the hub is unavailable, the
  service fails closed (`HTTP 503`) and never falls back to stale or corpus-wide data.
- **HTTP security boundaries:** `/mcp` requires a configured backend bearer token
  (>= 32 chars, constant-time `hmac.compare_digest` check, `401` on failure), a
  non-empty `X-Fleet-Subject` header (`403` on missing), `TrustedHostMiddleware`
  host validation, and `Cache-Control: no-store` on all API responses.
  `GET /health` runs SQLite `PRAGMA quick_check` (`200`/`503`) without auth.

## CLI reference

All commands are exposed via `filings-library`:

```sh
# Run one bounded sync pass
filings-library sync --data-dir ./data \
  --targets-url http://127.0.0.1:8000 \
  --targets-token "$FILINGS_TARGETS_TOKEN" \
  --extractor-url http://127.0.0.1:8081 \
  --sec-user-agent "FilingsLibrary/0.1 (contact@example.com)"

# Search, read, query XBRL facts, and inspect coverage (--person ID or --all)
filings-library search "operating margin" --data-dir ./data --all --json
filings-library read <document_id> --data-dir ./data --all --start page:1 --end page:3
filings-library facts Revenues NetIncomeLoss --data-dir ./data --all --json
filings-library status --data-dir ./data --all --json

# Maintenance & one-way Markdown export
filings-library export-md ./export --data-dir ./data
filings-library rebuild-fts --data-dir ./data

# Hosted servers
filings-library serve --data-dir ./data --host 0.0.0.0 --port 8080
filings-library extractor --host 0.0.0.0 --port 8081
```

## MCP tools

Mounted at `/mcp` (Streamable HTTP, stateless JSON responses):

| Tool | Description |
| --- | --- |
| `filings_list_companies` | List companies in the caller's allowed `source_keys`. |
| `filings_list_documents` | List documents filtered by `source_key`, `form_family`, `since`, `limit`. |
| `filings_search` | Scoped FTS5 financial search returning strict `Citation` dicts. |
| `filings_read` | Bounded section reader with `start_locator`, `end_locator`, and `max_chars`. |
| `filings_facts` | Query cached SEC XBRL `companyfacts` across the caller's allowed companies. |
| `filings_status` | Per-issuer coverage report (counts by `form_family`, adapters, latest filing date, `has_company_facts`) strictly for the caller's allowed companies. |
