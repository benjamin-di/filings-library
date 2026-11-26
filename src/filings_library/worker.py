"""Bounded sync orchestrator (`sync_once`) and one-way Markdown export projection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from filings_library.adapters import FilingCandidate
from filings_library.adapters.earnings_watch import EarningsWatchAdapter
from filings_library.adapters.esef_xbrl import EsefXbrlAdapter
from filings_library.adapters.hkexnews import HkexnewsAdapter
from filings_library.adapters.sec_edgar import SecEdgarAdapter
from filings_library.cas import ContentAddressedStore, SymlinkRejectedError
from filings_library.extractor.app import MAX_EXTRACT_BYTES, ExtractorClient
from filings_library.models import ArtifactKind, DocumentRecord, SourceAdapter
from filings_library.store import DEFAULT_GLOBAL_CAP_BYTES, FilingsStore
from filings_library.targets_client import LibraryTargetsClient
from filings_library.url_safety import (
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
    SourceAdapter.EARNINGS_WATCH: None,
}


@dataclass(frozen=True)
class SyncConfig:
    backfill_years: int = 3
    global_cap_bytes: int = DEFAULT_GLOBAL_CAP_BYTES
    per_issuer_cap_bytes: int = 512 * 1024 * 1024  # 512 MiB
    per_issuer_max_docs: int = 200
    sec_user_agent: str | None = None
    download_timeout: float = 60.0
    max_download_bytes: int = MAX_EXTRACT_BYTES


@dataclass
class SyncResult:
    companies_synced: int = 0
    documents_ingested: int = 0
    documents_skipped_existing: int = 0
    documents_skipped_quota: int = 0
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
    earnings_watch_adapter: EarningsWatchAdapter | None = None,
    download_transport: httpx.AsyncBaseTransport | None = None,
    url_resolver: HostResolver | None = None,
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

    sec_ua = sec.user_agent if sec is not None else cfg.sec_user_agent

    for company in payload.companies:
        store.upsert_company(company)
        result.companies_synced += 1

        candidates: list[FilingCandidate] = []
        # Priority order: release-day fast path first, then Tier 1/2 official adapters.
        adapter_list: list[tuple[str, object]] = [("earnings_watch", ew)]
        if sec is not None:
            adapter_list.append(("sec_edgar", sec))
        adapter_list.append(("hkexnews", hkex))
        adapter_list.append(("esef_xbrl", esef))

        for adapter_name, adapter in adapter_list:
            try:
                discovered = await adapter.discover(company, today=today)  # type: ignore[attr-defined]
                candidates.extend(discovered)
            except Exception as exc:
                result.errors.append(f"{company.source_key}/{adapter_name}: {exc}")

        # Process older filings before amendments so supersession links resolve cleanly.
        candidates.sort(key=lambda c: (c.is_amendment, c.filing_date, c.external_id))

        for cand in candidates:
            existing = store.get_document_by_external_id(
                cand.source_key, cand.adapter, cand.external_id
            )
            if existing is not None:
                result.documents_skipped_existing += 1
                continue

            if store.count_documents_for_issuer(company.source_key) >= cfg.per_issuer_max_docs:
                result.documents_skipped_quota += 1
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
            except Exception as exc:
                result.errors.append(f"download {cand.source_url}: {exc}")
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

            source_sha256 = cas.put_bytes(raw_bytes)
            mime_type = cand.source_mime or fetched_mime or "application/octet-stream"
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
                len(md_bytes),
                source_key=company.source_key,
                global_cap_bytes=cfg.global_cap_bytes,
                per_issuer_cap_bytes=cfg.per_issuer_cap_bytes,
                cas_bytes=cas.total_bytes(),
            )
            if not md_quota.allowed:
                result.documents_skipped_quota += 1
                continue

            md_sha256 = cas.put_bytes(md_bytes)
            store.put_artifact(
                source_sha256=source_sha256,
                kind=ArtifactKind.MARKDOWN,
                converter_id=extracted.converter_id,
                converter_version=extracted.converter_version,
                artifact_sha256=md_sha256,
                byte_size=len(md_bytes),
            )

            supersedes_id: str | None = None
            if cand.is_amendment and cand.base_form_type:
                supersedes_id = store.find_superseded_document_id(
                    source_key=company.source_key,
                    base_form_type=cand.base_form_type,
                    period_end=cand.period_end,
                    before_filing_date=cand.filing_date,
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
            store.upsert_document(doc_record)
            store.replace_sections(doc_id, extracted.sections)
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
