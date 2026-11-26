"""Tests for CAS, SQLite WAL store, FTS5 numeric normalization, financial ranking, and scoping."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from filings_library.cas import (
    ContentAddressedStore,
    CorruptedObjectError,
    SymlinkRejectedError,
)
from filings_library.models import (
    ArtifactKind,
    CompanyTarget,
    DocumentRecord,
    ExtractedSection,
    FormFamily,
    SourceAdapter,
)
from filings_library.search import DocumentNotFoundError, read_document, search_filings
from filings_library.store import FilingsStore, QuotaExceededError


def _make_company(source_key: str, ticker: str, name: str) -> CompanyTarget:
    return CompanyTarget(
        source_key=source_key,
        ticker=ticker,
        exchange="XNAS",
        name=name,
    )


def _make_doc(
    *,
    doc_id: str,
    source_key: str,
    external_id: str,
    form_type: str,
    form_family: FormFamily,
    filing_date: str,
    period_end: str | None = None,
    source_bytes: int = 1000,
    is_amendment: bool = False,
    supersedes_document_id: str | None = None,
) -> DocumentRecord:
    sha = hashlib.sha256(f"{doc_id}:{external_id}".encode()).hexdigest()
    return DocumentRecord(
        document_id=doc_id,
        source_key=source_key,
        adapter=SourceAdapter.SEC_EDGAR,
        external_id=external_id,
        form_type=form_type,
        form_family=form_family,
        title=f"{source_key} {form_type}",
        filing_date=filing_date,
        period_end=period_end,
        source_url=f"https://example.test/filings/{doc_id}.htm",
        source_sha256=sha,
        source_mime="text/html",
        source_bytes=source_bytes,
        is_amendment=is_amendment,
        supersedes_document_id=supersedes_document_id,
        converter_id="html-ixbrl",
        converter_version="1.0.0",
    )


def test_cas_immutability_integrity_and_symlink_rejection(tmp_path: Path) -> None:
    cas = ContentAddressedStore(tmp_path / "cas")
    payload = b"immutable filing bytes 22,594"
    digest = cas.put_bytes(payload)

    assert cas.has(digest)
    assert cas.get_bytes(digest) == payload
    assert cas.total_bytes() == len(payload)
    # Path layout: objects/sha256/<aa>/<sha256>
    expected_path = tmp_path / "cas" / "objects" / "sha256" / digest[:2] / digest
    assert expected_path.is_file()

    # Re-putting identical bytes is idempotent and does not increase total_bytes.
    assert cas.put_bytes(payload) == digest
    assert cas.total_bytes() == len(payload)

    # Corruption is caught on read.
    expected_path.write_bytes(b"tampered")
    with pytest.raises(CorruptedObjectError):
        cas.get_bytes(digest)

    # Symlink rejection on target path.
    other = tmp_path / "outside.txt"
    other.write_bytes(b"secret")
    fake_sha = "a" * 64
    shard = cas.objects_dir / "aa"
    shard.mkdir(parents=True, exist_ok=True)
    (shard / fake_sha).symlink_to(other)
    with pytest.raises(SymlinkRejectedError):
        cas.get_bytes(fake_sha)
    with pytest.raises(SymlinkRejectedError):
        cas.total_bytes()


def test_store_schema_fts_numeric_normalization_and_rebuild(tmp_path: Path) -> None:
    store = FilingsStore(tmp_path / "library.sqlite3")
    assert store.schema_version() == 1
    mode = store.conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert str(mode).lower() == "wal"

    store.upsert_company(_make_company("acme-corp", "ACME", "Acme Corp"))
    doc = _make_doc(
        doc_id="doc-10k",
        source_key="acme-corp",
        external_id="0001-25-0001",
        form_type="10-K",
        form_family=FormFamily.ANNUAL_REPORT,
        filing_date="2026-02-20",
        period_end="2025-12-31",
    )
    store.upsert_document(doc)
    store.replace_sections(
        "doc-10k",
        [
            ExtractedSection(
                locator="item:7",
                title="Item 7. Results of Operations",
                ordinal=1,
                markdown="Total consolidated revenue reached $22,594 million in FY2025.",
            )
        ],
    )

    # Both formatted "22,594" and unformatted "22594" match the same section.
    hits_comma = search_filings(store, "22,594", allowed_source_keys={"acme-corp"})
    hits_plain = search_filings(store, "22594", allowed_source_keys={"acme-corp"})
    assert len(hits_comma) == 1
    assert len(hits_plain) == 1
    assert hits_comma[0].document_id == "doc-10k"
    assert hits_plain[0].document_id == "doc-10k"
    assert hits_comma[0].locator == "item:7"
    assert hits_comma[0].source_sha256 == doc.source_sha256
    assert hits_comma[0].converter_id == "html-ixbrl"

    # Drop/corrupt FTS5 and verify rebuild_fts() deterministically restores it.
    store.conn.execute("DELETE FROM fts_chunks;")
    assert search_filings(store, "22594", allowed_source_keys={"acme-corp"}) == []
    rebuilt = store.rebuild_fts()
    assert rebuilt == 1
    hits_after = search_filings(store, "22,594", allowed_source_keys={"acme-corp"})
    assert len(hits_after) == 1
    assert hits_after[0].locator == "item:7"
    store.close()


def test_financial_ranking_recency_form_weight_amendment_and_boilerplate(tmp_path: Path) -> None:
    store = FilingsStore(tmp_path / "library.sqlite3")
    store.upsert_company(_make_company("alpha-inc", "ALPH", "Alpha Inc"))

    # 1. Form-family weight: annual_report outranks proxy on identical text and date.
    doc_annual = _make_doc(
        doc_id="doc-annual",
        source_key="alpha-inc",
        external_id="ann-2025",
        form_type="10-K",
        form_family=FormFamily.ANNUAL_REPORT,
        filing_date="2026-03-01",
        period_end="2025-12-31",
    )
    doc_proxy = _make_doc(
        doc_id="doc-proxy",
        source_key="alpha-inc",
        external_id="def14a-2025",
        form_type="DEF 14A",
        form_family=FormFamily.PROXY,
        filing_date="2026-03-01",
        period_end="2025-12-31",
    )
    store.upsert_document(doc_annual)
    store.upsert_document(doc_proxy)
    store.replace_sections(
        "doc-annual",
        [
            ExtractedSection(
                locator="item:7",
                title="Cloud Expansion",
                ordinal=1,
                markdown="Cloud infrastructure expansion accelerated enterprise bookings.",
            )
        ],
    )
    store.replace_sections(
        "doc-proxy",
        [
            ExtractedSection(
                locator="section:compensation",
                title="Executive Metrics",
                ordinal=1,
                markdown="Executive targets tied to cloud infrastructure expansion metrics.",
            )
        ],
    )
    hits = search_filings(
        store, "cloud infrastructure expansion", allowed_source_keys={"alpha-inc"}
    )
    assert len(hits) == 2
    assert hits[0].document_id == "doc-annual"
    assert hits[0].score > hits[1].score

    # 2. Amendment supersession: 10-K/A strongly outranks superseded 10-K.
    doc_orig = _make_doc(
        doc_id="doc-10k-orig",
        source_key="alpha-inc",
        external_id="10k-2024",
        form_type="10-K",
        form_family=FormFamily.ANNUAL_REPORT,
        filing_date="2025-02-28",
        period_end="2024-12-31",
    )
    doc_amend = _make_doc(
        doc_id="doc-10k-amend",
        source_key="alpha-inc",
        external_id="10ka-2024",
        form_type="10-K/A",
        form_family=FormFamily.ANNUAL_REPORT,
        filing_date="2025-03-15",
        period_end="2024-12-31",
        is_amendment=True,
        supersedes_document_id="doc-10k-orig",
    )
    store.upsert_document(doc_orig)
    store.upsert_document(doc_amend)
    store.replace_sections(
        "doc-10k-orig",
        [
            ExtractedSection(
                locator="item:8",
                title="Restatement Note",
                ordinal=1,
                markdown="Original deferred tax valuation allowance was 410 million.",
            )
        ],
    )
    store.replace_sections(
        "doc-10k-amend",
        [
            ExtractedSection(
                locator="item:8",
                title="Amended Restatement Note",
                ordinal=1,
                markdown="Amended deferred tax valuation allowance restated to 455 million.",
            )
        ],
    )
    amend_hits = search_filings(
        store, "deferred tax valuation allowance", allowed_source_keys={"alpha-inc"}
    )
    assert len(amend_hits) == 2
    assert amend_hits[0].document_id == "doc-10k-amend"
    assert amend_hits[0].score > amend_hits[1].score * 2.5

    # 3. Recency decay + near-duplicate boilerplate collapse across quarters for same company.
    boilerplate = (
        "Forward-looking statements involve known and unknown risks and uncertainties "
        "that may cause actual results to differ materially from management expectations."
    )
    for idx, fdate in enumerate(["2025-05-01", "2025-08-01", "2025-11-01"], start=1):
        qdoc = _make_doc(
            doc_id=f"doc-q{idx}",
            source_key="alpha-inc",
            external_id=f"10q-{idx}",
            form_type="10-Q",
            form_family=FormFamily.QUARTERLY_REPORT,
            filing_date=fdate,
        )
        store.upsert_document(qdoc)
        store.replace_sections(
            qdoc.document_id,
            [
                ExtractedSection(
                    locator="section:safe-harbor",
                    title="Safe Harbor",
                    ordinal=1,
                    markdown=boilerplate,
                )
            ],
        )

    bp_hits = search_filings(
        store, "forward-looking statements uncertainties", allowed_source_keys={"alpha-inc"}
    )
    # All three identical quarterly boilerplate sections collapse to the single newest one.
    assert len(bp_hits) == 1
    assert bp_hits[0].document_id == "doc-q3"
    store.close()


def test_strict_allowed_source_keys_isolation_and_read_document(tmp_path: Path) -> None:
    store = FilingsStore(tmp_path / "library.sqlite3")
    store.upsert_company(_make_company("pub-a", "PUBA", "Public A"))
    store.upsert_company(_make_company("pub-b", "PUBB", "Public B"))

    doc_a = _make_doc(
        doc_id="doc-a",
        source_key="pub-a",
        external_id="a1",
        form_type="10-K",
        form_family=FormFamily.ANNUAL_REPORT,
        filing_date="2026-02-01",
    )
    doc_b = _make_doc(
        doc_id="doc-b",
        source_key="pub-b",
        external_id="b1",
        form_type="10-K",
        form_family=FormFamily.ANNUAL_REPORT,
        filing_date="2026-02-01",
    )
    store.upsert_document(doc_a)
    store.upsert_document(doc_b)
    store.replace_sections(
        "doc-a",
        [
            ExtractedSection(
                locator="item:1", title="Business", ordinal=1, markdown="Quantum widget revenue."
            ),
            ExtractedSection(locator="item:7", title="MD&A", ordinal=2, markdown="X" * 300),
        ],
    )
    store.replace_sections(
        "doc-b",
        [
            ExtractedSection(
                locator="item:1",
                title="Business",
                ordinal=1,
                markdown="Quantum widget confidential.",
            )
        ],
    )

    # Empty allowed_source_keys -> empty result, never leaks.
    assert search_filings(store, "quantum widget", allowed_source_keys=set()) == []

    # Scoped to pub-a -> never sees pub-b even if source_key="pub-b" is explicitly requested.
    hits = search_filings(store, "quantum widget", allowed_source_keys={"pub-a"})
    assert [h.source_key for h in hits] == ["pub-a"]
    assert (
        search_filings(store, "quantum widget", allowed_source_keys={"pub-a"}, source_key="pub-b")
        == []
    )

    # read_document enforces allowed_source_keys and bounds max_chars.
    with pytest.raises(DocumentNotFoundError):
        read_document(store, document_id="doc-b", allowed_source_keys={"pub-a"})

    result = read_document(
        store,
        document_id="doc-a",
        allowed_source_keys={"pub-a"},
        start_locator="item:1",
        max_chars=120,
    )
    assert result["truncated"] is True
    assert result["total_chars"] == 120
    assert result["citations"][0]["locator"] == "item:1"
    assert result["citations"][0]["source_key"] == "pub-a"
    store.close()


def test_quota_enforcement_and_job_queue_dead_letter(tmp_path: Path) -> None:
    store = FilingsStore(tmp_path / "library.sqlite3")
    store.upsert_company(_make_company("cap-co", "CAP", "Cap Co"))
    doc = _make_doc(
        doc_id="doc-cap",
        source_key="cap-co",
        external_id="c1",
        form_type="10-K",
        form_family=FormFamily.ANNUAL_REPORT,
        filing_date="2026-02-01",
        source_bytes=800,
    )
    store.upsert_document(doc)
    store.put_artifact(
        source_sha256=doc.source_sha256,
        kind=ArtifactKind.MARKDOWN,
        converter_id="html-ixbrl",
        converter_version="1.0.0",
        artifact_sha256="b" * 64,
        byte_size=200,
    )
    usage = store.storage_usage("cap-co")
    assert usage == {"raw_bytes": 800, "artifact_bytes": 200, "total_bytes": 1000}

    with pytest.raises(QuotaExceededError):
        store.assert_quota(500, source_key="cap-co", global_cap_bytes=1200)
    with pytest.raises(QuotaExceededError):
        store.assert_quota(
            300, source_key="cap-co", global_cap_bytes=10_000, per_issuer_cap_bytes=1200
        )

    # Job queue retry and dead-letter transition.
    job_id = store.enqueue_job("extract", {"document_id": "doc-cap"}, max_attempts=2)
    leased1 = store.lease_job("worker-1")
    assert leased1 is not None and leased1["id"] == job_id and leased1["attempts"] == 1
    assert store.fail_job(job_id, "transient error") == "pending"

    leased2 = store.lease_job("worker-1")
    assert leased2 is not None and leased2["attempts"] == 2
    assert store.fail_job(job_id, "permanent error") == "dead"
    assert store.lease_job("worker-1") is None
    store.close()


def test_replace_sections_atomic_rollback_on_mid_write_error(tmp_path: Path) -> None:
    import sqlite3

    store = FilingsStore(tmp_path / "library.sqlite3")
    try:
        store.upsert_company(_make_company("tx-co", "TXC", "Transaction Co"))
        doc = _make_doc(
            doc_id="doc-tx",
            source_key="tx-co",
            external_id="tx-1",
            form_type="10-K",
            form_family=FormFamily.ANNUAL_REPORT,
            filing_date="2026-02-01",
        )
        store.upsert_document(doc)
        store.replace_sections(
            "doc-tx",
            [
                ExtractedSection(
                    locator="item:1",
                    title="Original Business",
                    ordinal=1,
                    markdown="Original durable revenue text.",
                )
            ],
        )

        # Duplicate locator in the second section triggers UNIQUE constraint mid-write.
        with pytest.raises(sqlite3.IntegrityError):
            store.replace_sections(
                "doc-tx",
                [
                    ExtractedSection(
                        locator="item:1",
                        title="Replacement",
                        ordinal=1,
                        markdown="Partial write that must roll back.",
                    ),
                    ExtractedSection(
                        locator="item:1",
                        title="Duplicate Locator",
                        ordinal=2,
                        markdown="Triggers IntegrityError.",
                    ),
                ],
            )

        # Original sections and FTS5 index must remain completely intact.
        sections = store.get_sections("doc-tx")
        assert len(sections) == 1
        assert sections[0].markdown == "Original durable revenue text."
        hits = search_filings(store, "durable", allowed_source_keys={"tx-co"})
        assert len(hits) == 1
        assert hits[0].document_id == "doc-tx"
        assert search_filings(store, "Partial", allowed_source_keys={"tx-co"}) == []
    finally:
        store.close()


def test_fts5_hostile_queries_and_limit_clamping(tmp_path: Path) -> None:
    store = FilingsStore(tmp_path / "library.sqlite3")
    try:
        store.upsert_company(_make_company("sec-co", "SEC", "Security Co"))
        for idx in range(3):
            doc = _make_doc(
                doc_id=f"doc-{idx}",
                source_key="sec-co",
                external_id=f"ext-{idx}",
                form_type="10-K",
                form_family=FormFamily.ANNUAL_REPORT,
                filing_date=f"2026-02-0{idx + 1}",
            )
            store.upsert_document(doc)
            store.replace_sections(
                doc.document_id,
                [
                    ExtractedSection(
                        locator="item:1",
                        title="Overview",
                        ordinal=1,
                        markdown=f"Unique term alpha{idx} and revenue growth " + ("z" * 200),
                    )
                ],
            )

        # Hostile FTS5 queries with NUL bytes, control chars, and unmatched quotes must not crash.
        for hostile in ["a\x00b", 'revenue "unclosed', "\x00\x01\x02\x1f\x7f", '"a\x00b"']:
            result = search_filings(store, hostile, allowed_source_keys={"sec-co"})
            assert isinstance(result, list)

        # Negative/zero limits are clamped to 1 (never SQLite LIMIT -1 returning all rows).
        assert len(store.list_documents({"sec-co"}, limit=-1)) == 1
        assert len(store.list_documents({"sec-co"}, limit=0)) == 1
        assert len(search_filings(store, "revenue", allowed_source_keys={"sec-co"}, limit=-1)) == 1

        # max_chars below 100 is clamped up to 100.
        read_res = read_document(
            store,
            document_id="doc-0",
            allowed_source_keys={"sec-co"},
            max_chars=-50,
        )
        assert read_res["total_chars"] == 100
        assert read_res["truncated"] is True
    finally:
        store.close()
