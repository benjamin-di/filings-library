"""Bounded sync orchestrator (`sync_once`) and one-way Markdown export projection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from filings_library.adapters import FilingCandidate
from filings_library.adapters.asx import AsxAdapter
from filings_library.adapters.earnings_watch import EarningsWatchAdapter, is_direct_audio_url
from filings_library.adapters.esef_xbrl import EsefXbrlAdapter
from filings_library.adapters.hkexnews import HkexnewsAdapter
from filings_library.adapters.sec_edgar import SecEdgarAdapter
from filings_library.cas import ContentAddressedStore, SymlinkRejectedError
from filings_library.extractor.app import MAX_EXTRACT_BYTES, ExtractorClient
from filings_library.models import (
    ArtifactKind,
    DocumentRecord,
    ExtractedDocument,
    ExtractedSection,
    FormFamily,
    SourceAdapter,
)
from filings_library.store import DEFAULT_GLOBAL_CAP_BYTES, FilingsStore, canonicalize_source_url
from filings_library.targets_client import LibraryTargetsClient
from filings_library.transcribe import (
    ASR_FORM_TYPE,
    normalize_audio_to_wav,
    parse_parakeet_segments,
    run_remote_parakeet,
    segments_to_extracted_document,
    ssh_parakeet_command,
)
from filings_library.url_safety import (
    ASX_ALLOWED_HOSTS,
    ESEF_ALLOWED_HOSTS,
    HKEX_ALLOWED_HOSTS,
    SEC_ALLOWED_HOSTS,
    HostResolver,
    validate_public_https_url,
)

_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_-]+")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_DOWNLOAD_REDIRECTS = 5

_ADAPTER_ALLOWED_HOSTS: dict[SourceAdapter, frozenset[str] | None] = {
    SourceAdapter.SEC_EDGAR: SEC_ALLOWED_HOSTS,
    SourceAdapter.HKEXNEWS: HKEX_ALLOWED_HOSTS,
    SourceAdapter.ESEF_XBRL: ESEF_ALLOWED_HOSTS,
    SourceAdapter.ASX: ASX_ALLOWED_HOSTS,
    SourceAdapter.EARNINGS_WATCH: None,
}


DEFAULT_PER_ISSUER_CAP_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB
DEFAULT_PER_ISSUER_MAX_DOCS = 500
_GENERIC_MIME_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})


@dataclass(frozen=True)
class SyncConfig:
    backfill_years: int = 3
    global_cap_bytes: int = DEFAULT_GLOBAL_CAP_BYTES
    per_issuer_cap_bytes: int = DEFAULT_PER_ISSUER_CAP_BYTES
    per_issuer_max_docs: int = DEFAULT_PER_ISSUER_MAX_DOCS
    sec_user_agent: str | None = None
    download_timeout: float = 60.0
    max_download_bytes: int = MAX_EXTRACT_BYTES
    parakeet_ssh_config: str | None = None
    max_transcriptions_per_sync: int = 5


@dataclass
class SyncResult:
    companies_synced: int = 0
    documents_ingested: int = 0
    documents_skipped_existing: int = 0
    documents_skipped_quota: int = 0
    transcriptions_queued: int = 0
    transcriptions_completed: int = 0
    facts_updated: int = 0
    errors: list[str] = field(default_factory=list)


def _make_document_id(source_key: str, adapter: str, external_id: str) -> str:
    raw = f"{source_key}|{adapter}|{external_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    return name or "document"


def _sanitize_path_component(value: str, *, fallback: str) -> str:
    stripped = (value or "").strip()
    if not stripped or stripped in {".", ".."}:
        return fallback
    cleaned = _SAFE_COMPONENT_RE.sub("_", stripped).strip("_")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned


async def _download_bytes(
    url: str,
    *,
    sec_user_agent: str | None,
    transport: httpx.AsyncBaseTransport | None,
    timeout: float,
    max_bytes: int = MAX_EXTRACT_BYTES,
    allowed_hosts: Collection[str] | None = None,
    resolver: HostResolver | None = None,
    max_redirects: int = MAX_DOWNLOAD_REDIRECTS,
) -> tuple[bytes, str]:
    current_url = url
    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        for hop in range(max_redirects + 1):
            validated_url = validate_public_https_url(
                current_url,
                allowed_hosts=allowed_hosts,
                resolver=resolver,
            )
            host = (urlparse(validated_url).hostname or "").lower().rstrip(".")
            headers: dict[str, str] = {}
            if sec_user_agent and (host == "sec.gov" or host.endswith(".sec.gov")):
                headers["User-Agent"] = sec_user_agent

            async with client.stream("GET", validated_url, headers=headers) as resp:
                if resp.status_code in _REDIRECT_STATUSES and "location" in resp.headers:
                    if hop >= max_redirects:
                        raise ValueError(f"Exceeded max redirects ({max_redirects}) for {url}")
                    current_url = urljoin(validated_url, resp.headers["location"])
                    continue

                resp.raise_for_status()

                content_length = resp.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length.strip())
                    except ValueError:
                        declared_size = -1
                    if declared_size > max_bytes:
                        raise ValueError(
                            f"Download Content-Length {declared_size} exceeds max_bytes ({max_bytes})"
                        )

                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    if len(buf) + len(chunk) > max_bytes:
                        raise ValueError(f"Download body exceeded max_bytes ({max_bytes})")
                    buf.extend(chunk)

                content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                return bytes(buf), content_type

    raise ValueError(f"Exceeded max redirects ({max_redirects}) for {url}")


async def _drain_audio_transcriptions(
    *,
    store: FilingsStore,
    cas: ContentAddressedStore,
    cfg: SyncConfig,
    result: SyncResult,
    download_transport: httpx.AsyncBaseTransport | None,
    url_resolver: HostResolver | None,
    transcriber: Callable[[bytes, str], Awaitable[tuple[bytes, ExtractedDocument]]] | None,
) -> None:
    ssh_cfg = (
        cfg.parakeet_ssh_config or os.environ.get("FILINGS_PARAKEET_WORKER_SSH_CONFIG", "").strip()
    )
    if transcriber is None and not ssh_cfg:
        return

    processed = 0
    while processed < cfg.max_transcriptions_per_sync:
        job = store.lease_job("filings-worker-asr", lease_seconds=1800)
        if job is None:
            break
        if job["kind"] != "audio_transcribe":
            # Re-queue non-ASR jobs for their dedicated handler without burning attempts
            store.conn.execute(
                "UPDATE jobs SET status='pending', attempts=MAX(0, attempts-1), lease_owner=NULL, lease_expires_at=NULL WHERE id=?",
                (job["id"],),
            )
            break
        processed += 1
        payload = job["payload"]
        source_key = str(payload["source_key"])
        external_id = str(payload["external_id"])
        source_url = str(payload["source_url"])
        title = str(payload.get("title") or "Earnings Call Transcript")
        filing_date = str(payload.get("filing_date") or "undated")
        period_end = payload.get("period_end")
        try:
            if store.has_official_transcript(
                source_key=source_key, period_end=period_end, filing_date=filing_date
            ):
                store.complete_job(job["id"])
                continue
            raw_bytes, fetched_mime = await _download_bytes(
                source_url,
                sec_user_agent=cfg.sec_user_agent,
                transport=download_transport,
                timeout=max(cfg.download_timeout, 180.0),
                max_bytes=cfg.max_download_bytes,
                allowed_hosts=None,
                resolver=url_resolver,
            )
            audio_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            if transcriber is not None:
                asr_json_bytes, extracted = await transcriber(
                    raw_bytes, _filename_from_url(source_url)
                )
            else:
                scratch_root = Path(os.environ.get("TMPDIR", "/scratch"))
                scratch_dir = scratch_root if scratch_root.exists() else Path(tempfile.gettempdir())
                with tempfile.TemporaryDirectory(prefix="filings-asr-", dir=scratch_dir) as tmpdir:
                    tdir = Path(tmpdir)
                    raw_path = tdir / (_filename_from_url(source_url) or "audio.bin")
                    wav_path = tdir / "audio.wav"
                    out_json = tdir / "segments.json"
                    raw_path.write_bytes(raw_bytes)
                    await normalize_audio_to_wav(raw_path, wav_path)
                    prov = await run_remote_parakeet(
                        wav_path, out_json, command=ssh_parakeet_command(ssh_cfg)
                    )
                    asr_json_bytes = out_json.read_bytes()
                    segments = parse_parakeet_segments(asr_json_bytes)
                    extracted = segments_to_extracted_document(
                        segments,
                        converter_version=f"{prov.engine_version}+{prov.model_sha256[:12]}",
                    )

            md_bytes = extracted.markdown.encode("utf-8")
            quota = store.check_quota(
                len(asr_json_bytes) + len(md_bytes),
                source_key=source_key,
                global_cap_bytes=cfg.global_cap_bytes,
                per_issuer_cap_bytes=cfg.per_issuer_cap_bytes,
                cas_bytes=cas.total_bytes(),
            )
            if not quota.allowed:
                store.fail_job(job["id"], quota.reason or "quota_exceeded")
                result.documents_skipped_quota += 1
                continue

            # Discard raw audio; persist immutable raw ASR JSON + Markdown in CAS
            asr_sha256 = cas.put_bytes(asr_json_bytes)
            md_sha256 = cas.put_bytes(md_bytes)
            doc_id = _make_document_id(source_key, str(SourceAdapter.EARNINGS_WATCH), external_id)
            doc_record = DocumentRecord(
                document_id=doc_id,
                source_key=source_key,
                adapter=SourceAdapter.EARNINGS_WATCH,
                external_id=external_id,
                form_type=ASR_FORM_TYPE,
                form_family=FormFamily.TRANSCRIPT,
                title=title,
                filing_date=filing_date,
                period_end=period_end,
                language="en",
                source_url=source_url,
                source_sha256=audio_sha256,
                source_mime=fetched_mime or "audio/mpeg",
                source_bytes=len(raw_bytes),
                refetchable=False,
                is_amendment=False,
                supersedes_document_id=None,
                converter_id=extracted.converter_id,
                converter_version=extracted.converter_version,
                markdown_sha256=md_sha256,
                page_count=None,
            )
            with store.transaction():
                store.put_artifact(
                    source_sha256=audio_sha256,
                    kind=ArtifactKind.ASR_JSON,
                    converter_id=extracted.converter_id,
                    converter_version=extracted.converter_version,
                    artifact_sha256=asr_sha256,
                    byte_size=len(asr_json_bytes),
                )
                store.put_artifact(
                    source_sha256=audio_sha256,
                    kind=ArtifactKind.MARKDOWN,
                    converter_id=extracted.converter_id,
                    converter_version=extracted.converter_version,
                    artifact_sha256=md_sha256,
                    byte_size=len(md_bytes),
                )
                store.upsert_document(doc_record)
                store.replace_sections(doc_id, extracted.sections)
            store.complete_job(job["id"])
            result.transcriptions_completed += 1
            result.documents_ingested += 1
        except Exception as exc:
            store.fail_job(job["id"], str(exc))
            result.errors.append(f"transcribe {external_id}: {exc}")


async def sync_once(
    *,
    store: FilingsStore,
    cas: ContentAddressedStore,
    targets_client: LibraryTargetsClient,
    extractor_client: ExtractorClient,
    config: SyncConfig | None = None,
    sec_adapter: SecEdgarAdapter | None = None,
    hkex_adapter: HkexnewsAdapter | None = None,
    esef_adapter: EsefXbrlAdapter | None = None,
    asx_adapter: AsxAdapter | None = None,
    earnings_watch_adapter: EarningsWatchAdapter | None = None,
    download_transport: httpx.AsyncBaseTransport | None = None,
    url_resolver: HostResolver | None = None,
    transcriber: Callable[[bytes, str], Awaitable[tuple[bytes, ExtractedDocument]]] | None = None,
    today: date | None = None,
) -> SyncResult:
    """Run one bounded sync pass across all companies returned by `targets_client`."""
    cfg = config or SyncConfig()
    result = SyncResult()

    payload = await targets_client.fetch_targets()

    ew = earnings_watch_adapter or EarningsWatchAdapter(resolver=url_resolver)
    sec = sec_adapter
    if sec is None and cfg.sec_user_agent:
        sec = SecEdgarAdapter(
            user_agent=cfg.sec_user_agent,
            backfill_years=cfg.backfill_years,
            transport=download_transport,
            resolver=url_resolver,
            store=store,
        )
    hkex = hkex_adapter or HkexnewsAdapter(
        backfill_years=cfg.backfill_years,
        transport=download_transport,
        resolver=url_resolver,
    )
    esef = esef_adapter or EsefXbrlAdapter(
        backfill_years=cfg.backfill_years,
        transport=download_transport,
        resolver=url_resolver,
    )
    asx = asx_adapter or AsxAdapter(
        backfill_years=cfg.backfill_years,
        transport=download_transport,
        resolver=url_resolver,
    )

    sec_ua = sec.user_agent if sec is not None else cfg.sec_user_agent

    for company in payload.companies:
        store.upsert_company(company)
        result.companies_synced += 1

        candidates: list[FilingCandidate] = []
        # Priority order: Earnings Watch hub (verified_documents + ir_assets),
        # then official regulatory registries (SEC, HKEX, ESEF, ASX).
        adapter_list: list[tuple[str, object]] = [("earnings_watch", ew)]
        if sec is not None:
            adapter_list.append(("sec_edgar", sec))
        adapter_list.append(("hkexnews", hkex))
        adapter_list.append(("esef_xbrl", esef))
        adapter_list.append(("asx", asx))

        for adapter_name, adapter in adapter_list:
            try:
                discovered = await adapter.discover(company, today=today)  # type: ignore[attr-defined]
                candidates.extend(discovered)
            except Exception as exc:
                result.errors.append(f"{company.source_key}/{adapter_name}: {exc}")

        # Deduplicate against existing DB entries and canonical URLs within this batch first.
        pending_candidates: list[FilingCandidate] = []
        seen_batch_urls: set[str] = set()
        for cand in candidates:
            existing = store.get_document_by_external_id(
                cand.source_key, cand.adapter, cand.external_id
            )
            if existing is not None:
                result.documents_skipped_existing += 1
                continue
            canon_url = canonicalize_source_url(cand.source_url)
            if (canon_url and canon_url in seen_batch_urls) or store.has_document_by_url(
                cand.source_key, cand.source_url
            ):
                result.documents_skipped_existing += 1
                continue
            if canon_url:
                seen_batch_urls.add(canon_url)
            pending_candidates.append(cand)

        # Apply per_issuer_max_docs budget prioritizing non-CURRENT_REPORT filings first
        # and newest-first within each tier so routine 6-K/8-K spam never starves annual,
        # quarterly, or results filings.
        existing_count = store.count_documents_for_issuer(company.source_key)
        remaining_slots = max(0, cfg.per_issuer_max_docs - existing_count)
        if len(pending_candidates) > remaining_slots:
            # Sort non-CURRENT_REPORT newest-first, then CURRENT_REPORT newest-first.
            non_current = [
                c for c in pending_candidates if c.form_family != FormFamily.CURRENT_REPORT
            ]
            current_reports = [
                c for c in pending_candidates if c.form_family == FormFamily.CURRENT_REPORT
            ]
            non_current.sort(
                key=lambda c: ("" if c.filing_date == "undated" else c.filing_date, c.external_id),
                reverse=True,
            )
            current_reports.sort(
                key=lambda c: ("" if c.filing_date == "undated" else c.filing_date, c.external_id),
                reverse=True,
            )
            prioritized = non_current + current_reports
            selected_candidates = prioritized[:remaining_slots]
            result.documents_skipped_quota += len(prioritized) - len(selected_candidates)
        else:
            selected_candidates = pending_candidates

        # Process the selected batch oldest-first (and base forms before amendments)
        # so amendment `supersedes_document_id` links still resolve cleanly.
        selected_candidates.sort(key=lambda c: (c.is_amendment, c.filing_date, c.external_id))

        for cand in selected_candidates:
            if store.count_documents_for_issuer(company.source_key) >= cfg.per_issuer_max_docs:
                result.documents_skipped_quota += 1
                continue

            if cand.form_type == "earnings_call_audio_replay" and is_direct_audio_url(
                cand.source_url
            ):
                if store.has_official_transcript(
                    source_key=company.source_key,
                    period_end=cand.period_end,
                    filing_date=cand.filing_date,
                ):
                    result.documents_skipped_existing += 1
                    continue
                queued_id = store.enqueue_job_if_absent(
                    "audio_transcribe",
                    {
                        "source_key": cand.source_key,
                        "external_id": cand.external_id,
                        "source_url": cand.source_url,
                        "title": cand.title,
                        "filing_date": cand.filing_date,
                        "period_end": cand.period_end,
                    },
                )
                if queued_id is not None:
                    result.transcriptions_queued += 1
                continue

            if cand.form_type in {"webcast_replay", "earnings_call_audio_replay"}:
                doc_id = _make_document_id(cand.source_key, str(cand.adapter), cand.external_id)
                md_text = f"## {cand.title}\n\nOfficial webcast replay: [{cand.source_url}]({cand.source_url})\n"
                md_bytes = md_text.encode("utf-8")
                md_sha256 = cas.put_bytes(md_bytes)
                link_sha256 = hashlib.sha256(cand.source_url.encode("utf-8")).hexdigest()
                webcast_doc = DocumentRecord(
                    document_id=doc_id,
                    source_key=cand.source_key,
                    adapter=cand.adapter,
                    external_id=cand.external_id,
                    form_type="webcast_replay",
                    form_family=FormFamily.WEBCAST,
                    title=cand.title,
                    filing_date=cand.filing_date,
                    period_end=cand.period_end,
                    language=cand.language,
                    source_url=cand.source_url,
                    source_sha256=link_sha256,
                    source_mime="text/html",
                    source_bytes=len(md_bytes),
                    refetchable=False,
                    is_amendment=False,
                    converter_id="webcast-bookmark",
                    converter_version="1.0.0",
                    markdown_sha256=md_sha256,
                    page_count=None,
                )
                with store.transaction():
                    store.put_artifact(
                        source_sha256=link_sha256,
                        kind=ArtifactKind.MARKDOWN,
                        converter_id="webcast-bookmark",
                        converter_version="1.0.0",
                        artifact_sha256=md_sha256,
                        byte_size=len(md_bytes),
                    )
                    store.upsert_document(webcast_doc)
                    store.replace_sections(
                        doc_id,
                        [
                            ExtractedSection(
                                locator="section:webcast",
                                title=cand.title,
                                ordinal=0,
                                markdown=md_text,
                                ocr_used=False,
                            )
                        ],
                    )
                result.documents_ingested += 1
                continue

            try:
                raw_bytes, fetched_mime = await _download_bytes(
                    cand.source_url,
                    sec_user_agent=sec_ua,
                    transport=download_transport,
                    timeout=cfg.download_timeout,
                    max_bytes=cfg.max_download_bytes,
                    allowed_hosts=_ADAPTER_ALLOWED_HOSTS.get(cand.adapter),
                    resolver=url_resolver,
                )
            except httpx.HTTPStatusError as exc:
                download_ir_asset = getattr(targets_client, "download_ir_asset", None)
                if (
                    exc.response.status_code == 403
                    and cand.adapter == SourceAdapter.EARNINGS_WATCH
                    and cand.ir_asset_id
                    and callable(download_ir_asset)
                ):
                    try:
                        raw_bytes, fetched_mime = await targets_client.download_ir_asset(
                            cand.ir_asset_id,
                            max_bytes=cfg.max_download_bytes,
                            timeout=cfg.download_timeout,
                        )
                    except Exception as fallback_exc:
                        result.errors.append(
                            f"download {cand.source_url}: {exc}; "
                            f"ir_asset fallback {cand.ir_asset_id}: {fallback_exc}"
                        )
                        continue
                else:
                    result.errors.append(f"download {cand.source_url}: {exc}")
                    continue
            except Exception as exc:
                result.errors.append(f"download {cand.source_url}: {exc}")
                continue

            raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            if store.has_document_by_sha256(company.source_key, raw_sha256):
                result.documents_skipped_existing += 1
                continue

            quota = store.check_quota(
                len(raw_bytes),
                source_key=company.source_key,
                global_cap_bytes=cfg.global_cap_bytes,
                per_issuer_cap_bytes=cfg.per_issuer_cap_bytes,
                cas_bytes=cas.total_bytes(),
            )
            if not quota.allowed:
                result.documents_skipped_quota += 1
                continue

            mime_type = (
                fetched_mime
                if fetched_mime not in _GENERIC_MIME_TYPES
                else (cand.source_mime or fetched_mime or "application/octet-stream")
            )
            filename = _filename_from_url(cand.source_url)

            try:
                extracted = await extractor_client.extract(
                    raw_bytes, mime_type=mime_type, filename=filename
                )
            except Exception as exc:
                result.errors.append(f"extract {cand.external_id}: {exc}")
                continue

            md_bytes = extracted.markdown.encode("utf-8")
            md_quota = store.check_quota(
                len(raw_bytes) + len(md_bytes),
                source_key=company.source_key,
                global_cap_bytes=cfg.global_cap_bytes,
                per_issuer_cap_bytes=cfg.per_issuer_cap_bytes,
                cas_bytes=cas.total_bytes(),
            )
            if not md_quota.allowed:
                result.documents_skipped_quota += 1
                continue

            source_sha256 = cas.put_bytes(raw_bytes)
            md_sha256 = cas.put_bytes(md_bytes)

            supersedes_id: str | None = None
            if cand.is_amendment and cand.base_form_type:
                supersedes_id = store.find_superseded_document_id(
                    source_key=company.source_key,
                    base_form_type=cand.base_form_type,
                    period_end=cand.period_end,
                    before_filing_date=cand.filing_date,
                )
            elif cand.form_family == FormFamily.TRANSCRIPT and cand.form_type != ASR_FORM_TYPE:
                supersedes_id = store.find_asr_transcript_to_supersede(
                    source_key=company.source_key,
                    period_end=cand.period_end,
                    filing_date=cand.filing_date,
                )

            doc_id = _make_document_id(cand.source_key, str(cand.adapter), cand.external_id)
            doc_record = DocumentRecord(
                document_id=doc_id,
                source_key=cand.source_key,
                adapter=cand.adapter,
                external_id=cand.external_id,
                form_type=cand.form_type,
                form_family=cand.form_family,
                title=cand.title,
                filing_date=cand.filing_date,
                period_end=cand.period_end,
                language=cand.language,
                source_url=cand.source_url,
                source_sha256=source_sha256,
                source_mime=mime_type,
                source_bytes=len(raw_bytes),
                refetchable=cand.refetchable,
                is_amendment=cand.is_amendment,
                supersedes_document_id=supersedes_id,
                converter_id=extracted.converter_id,
                converter_version=extracted.converter_version,
                markdown_sha256=md_sha256,
                page_count=extracted.page_count,
            )
            try:
                with store.transaction():
                    store.put_artifact(
                        source_sha256=source_sha256,
                        kind=ArtifactKind.MARKDOWN,
                        converter_id=extracted.converter_id,
                        converter_version=extracted.converter_version,
                        artifact_sha256=md_sha256,
                        byte_size=len(md_bytes),
                    )
                    store.upsert_document(doc_record)
                    store.replace_sections(doc_id, extracted.sections)
            except Exception as exc:
                result.errors.append(f"persist {cand.external_id}: {exc}")
                continue
            result.documents_ingested += 1

        # Fetch SEC XBRL companyfacts if company has a CIK and SEC adapter is active.
        if sec is not None and company.sec_cik is not None:
            try:
                facts_json = await sec.fetch_company_facts(company.sec_cik)
                facts_bytes = json.dumps(facts_json, sort_keys=True).encode("utf-8")
                facts_quota = store.check_quota(
                    len(facts_bytes),
                    source_key=company.source_key,
                    global_cap_bytes=cfg.global_cap_bytes,
                    per_issuer_cap_bytes=cfg.per_issuer_cap_bytes,
                    cas_bytes=cas.total_bytes(),
                )
                if facts_quota.allowed:
                    facts_sha256 = cas.put_bytes(facts_bytes)
                    store.put_artifact(
                        source_sha256=facts_sha256,
                        kind=ArtifactKind.FACTS_JSON,
                        converter_id="sec-companyfacts",
                        converter_version="1.0.0",
                        artifact_sha256=facts_sha256,
                        byte_size=len(facts_bytes),
                    )
                    store.upsert_company_facts(company.source_key, company.sec_cik, facts_sha256)
                    result.facts_updated += 1
                else:
                    result.documents_skipped_quota += 1
            except Exception as exc:
                result.errors.append(f"{company.source_key}/companyfacts: {exc}")

    await _drain_audio_transcriptions(
        store=store,
        cas=cas,
        cfg=cfg,
        result=result,
        download_transport=download_transport,
        url_resolver=url_resolver,
        transcriber=transcriber,
    )

    return result


def export_markdown(
    store: FilingsStore,
    cas: ContentAddressedStore,
    out_dir: Path | str,
    *,
    allowed_source_keys: Collection[str] | None = None,
) -> list[Path]:
    """One-way disposable Markdown projection from CAS/SQLite to `out_dir`.

    Never reads back into SQLite. Rejects symlinks and path traversal.
    """
    target_root = Path(out_dir)
    if target_root.is_symlink():
        raise SymlinkRejectedError(f"Export output directory must not be a symlink: {target_root}")
    target_root.mkdir(parents=True, exist_ok=True)
    if target_root.is_symlink():
        raise SymlinkRejectedError(f"Export output directory must not be a symlink: {target_root}")
    resolved_root = target_root.resolve()

    if allowed_source_keys is None:
        rows = store.conn.execute("SELECT source_key FROM companies ORDER BY source_key").fetchall()
        keys = [str(r["source_key"]) for r in rows]
    else:
        keys = sorted(set(allowed_source_keys))

    docs = store.list_all_documents(keys)
    written: list[Path] = []

    for doc in docs:
        if doc.markdown_sha256 and cas.has(doc.markdown_sha256):
            md_text = cas.get_bytes(doc.markdown_sha256).decode("utf-8", errors="replace")
        else:
            sections = store.get_sections(doc.document_id)
            md_text = "\n\n".join(s.markdown for s in sections) + "\n"

        safe_key = _sanitize_path_component(doc.source_key, fallback="issuer")
        safe_date = _sanitize_path_component(doc.filing_date, fallback="undated")
        safe_family = _sanitize_path_component(str(doc.form_family.value), fallback="other")
        safe_doc_id = _sanitize_path_component(doc.document_id, fallback="doc")

        issuer_dir = target_root / safe_key
        if issuer_dir.is_symlink():
            raise SymlinkRejectedError(
                f"Export issuer directory must not be a symlink: {issuer_dir}"
            )
        issuer_dir.mkdir(parents=True, exist_ok=True)
        if issuer_dir.is_symlink():
            raise SymlinkRejectedError(
                f"Export issuer directory must not be a symlink: {issuer_dir}"
            )
        if not issuer_dir.resolve().is_relative_to(resolved_root):
            raise ValueError(f"Export issuer directory escapes out_dir: {issuer_dir}")

        out_path = issuer_dir / f"{safe_date}_{safe_family}_{safe_doc_id}.md"
        if out_path.is_symlink():
            raise SymlinkRejectedError(f"Export target file must not be a symlink: {out_path}")
        if not out_path.resolve().is_relative_to(resolved_root):
            raise ValueError(f"Export target file escapes out_dir: {out_path}")

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=issuer_dir,
                prefix=".export_",
                suffix=".tmp",
                delete=False,
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(md_text)
                tmp_file.flush()
            if out_path.is_symlink():
                raise SymlinkRejectedError(f"Export target file must not be a symlink: {out_path}")
            os.replace(tmp_path, out_path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

        written.append(out_path)

    return written
