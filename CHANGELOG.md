# Changelog

## [0.6.0] - 2026-11-28

### Changed
- Retire and remove legacy `IssuerIrAdapter` and purge leftover `issuer_ir` document/section/FTS rows and `ir_gap_backfill` jobs on startup without touching CAS blobs.
- Reclassify routine SEC `6-K` share/governance noise (buybacks, TVR, PDMR, block listings, holdings) as `current_report` while guarding financial results headlines.
- Prioritize `per_issuer_max_docs` newest-first while preserving oldest-first amendment supersession, deduplicate cross-adapter URLs and SHA-256 content hashes, check HKEXnews multi-tag financial results before overseas regulatory announcements, and deduplicate ESEF filings by `(period_end, language)`.

### Added
- Add SSRF-safe `AsxAdapter` using MarkitDigital JSON API with ASX HTML fallback for ASX-listed issuers.
- Add WAF HTTP 403 fallback via `LibraryTargetsClient.download_ir_asset()` (`GET /internal/ir-assets/{asset_id}/download`) strictly for verified `earnings_watch` IR asset candidates.
- Extract Item 2.02 `8-K` `EX-99.1` earnings exhibits with SQLite `sec_index_cache` caching.

### Upgrade
- Action required: none.
- Rollback: compatible with 0.5.2 (startup migration purges retired `issuer_ir` rows and `ir_gap_backfill` jobs and creates `sec_index_cache` idempotently without modifying CAS blobs).

## [0.5.2] - 2026-11-28

### Fixed
- Parse native `local-transcriber` Parakeet worker JSON wire format (`transcription` with millisecond `offsets.from`/`offsets.to`) in `parse_parakeet_segments()`.

### Upgrade
- Action required: none.
- Rollback: compatible with 0.5.1.

## [0.5.1] - 2026-11-28

### Fixed
- Publish `release/compatibility.json` metadata for the 0.5.x IR presentations, transcripts, PPTX, and Parakeet ASR release line.

### Upgrade
- Action required: none.
- Rollback: compatible with 0.4.2.

## [0.5.0] - 2026-11-28

### Added

- Request `/internal/library-targets?schema=2` and ingest non-event `ir_assets` (presentations, official transcripts, audio replays, and webcast bookmarks) with true `published_date` and `period`.
- Add MIT `python-pptx` presentation extraction (`slide:N` locators) in `filings-extractor`.
- Add `audio_transcribe` job draining over the bounded Parakeet v1 stdio protocol (`ts:MM:SS` locators, immutable `asr_json` + Markdown in CAS, raw audio discarded after SHA-256 provenance capture).
- Add `Self-Transcribed (ASR)` vs `Official Transcript` display badges, `transcript_origin` metadata, and automatic supersession of ASR transcripts when an official transcript arrives.
- Retire redundant HTML-scraping `IssuerIrAdapter` from the default sync loop.

### Upgrade

- Action required: optional — mount `FILINGS_PARAKEET_WORKER_SSH_CONFIG` to enable self-transcription of earnings call audio replays.
- Rollback: compatible with 0.4.2.

## [0.4.2] - 2026-11-27

### Fixed

- Raise uvicorn HTTP keep-alive timeout to 65 seconds so reverse proxies can reuse idle upstream connections across 60-second windows without hitting the 5-second default timeout.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.4.1.
