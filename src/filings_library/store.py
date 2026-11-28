"""SQLite WAL metadata, sections, FTS5 search index, jobs, and quota store."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from filings_library.models import (
    ArtifactKind,
    CompanyTarget,
    DocumentRecord,
    ExtractedSection,
    FormFamily,
    SourceAdapter,
)

SCHEMA_VERSION = 1
DEFAULT_GLOBAL_CAP_BYTES = 100 * 1024 * 1024 * 1024  # 100 GiB
MIN_QUERY_LIMIT = 1
MAX_QUERY_LIMIT = 200

_COMMA_NUMBER_RE = re.compile(r"(?<!\d)\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d)")


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_normalized_numbers(text: str) -> str:
    """Return space-separated comma-stripped tokens for formatted numbers in `text`.

    For example, `"Revenue was $22,594.50 million"` produces `"22594.50 22594"`.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for match in _COMMA_NUMBER_RE.finditer(text):
        stripped = match.group(0).replace(",", "")
        if stripped not in seen:
            seen.add(stripped)
            tokens.append(stripped)
        if "." in stripped:
            int_part = stripped.split(".", 1)[0]
            if int_part not in seen:
                seen.add(int_part)
                tokens.append(int_part)
    return " ".join(tokens)


class QuotaExceededError(RuntimeError):
    """Raised when adding bytes would exceed global or per-issuer storage quotas."""


@dataclass(frozen=True)
class QuotaStatus:
    allowed: bool
    global_used_bytes: int
    global_cap_bytes: int
    issuer_used_bytes: int
    issuer_cap_bytes: int | None
    reason: str | None = None


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS companies (
    source_key TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    isin TEXT,
    sec_cik INTEGER,
    hkex_stock_id TEXT,
    lei TEXT,
    ir_urls_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL REFERENCES companies(source_key) ON DELETE CASCADE,
    adapter TEXT NOT NULL,
    external_id TEXT NOT NULL,
    form_type TEXT NOT NULL,
    form_family TEXT NOT NULL,
    title TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    period_end TEXT,
    language TEXT NOT NULL DEFAULT 'en',
    source_url TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_mime TEXT NOT NULL,
    source_bytes INTEGER NOT NULL,
    refetchable INTEGER NOT NULL DEFAULT 1,
    is_amendment INTEGER NOT NULL DEFAULT 0,
    supersedes_document_id TEXT REFERENCES documents(document_id) ON DELETE SET NULL,
    converter_id TEXT,
    converter_version TEXT,
    markdown_sha256 TEXT,
    page_count INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(source_key, adapter, external_id)
);

CREATE INDEX IF NOT EXISTS idx_documents_source_key_date
    ON documents(source_key, filing_date DESC);

CREATE INDEX IF NOT EXISTS idx_documents_supersedes
    ON documents(supersedes_document_id);

CREATE TABLE IF NOT EXISTS artifacts (
    source_sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    converter_id TEXT NOT NULL,
    converter_version TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(source_sha256, kind, converter_id, converter_version)
);

CREATE TABLE IF NOT EXISTS sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    locator TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL,
    markdown TEXT NOT NULL,
    ocr_used INTEGER NOT NULL DEFAULT 0,
    UNIQUE(document_id, locator)
);

CREATE INDEX IF NOT EXISTS idx_sections_doc_ordinal
    ON sections(document_id, ordinal);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
    title,
    body,
    normalized_numbers,
    document_id UNINDEXED,
    source_key UNINDEXED,
    locator UNINDEXED,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS company_facts (
    source_key TEXT PRIMARY KEY REFERENCES companies(source_key) ON DELETE CASCADE,
    cik INTEGER NOT NULL,
    facts_sha256 TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_lease
    ON jobs(status, lease_expires_at);
"""


class FilingsStore:
    """SQLite WAL store for filings metadata, sections, FTS5 chunks, artifacts, and jobs."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure_pragmas()
        self._init_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def _configure_pragmas(self) -> None:
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA busy_timeout = 5000;")
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")

    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> FilingsStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run multiple store writes atomically in one `BEGIN IMMEDIATE` transaction."""
        with self._tx():
            yield

    @contextmanager
    def _tx(self) -> Iterator[None]:
        if self._conn.in_transaction:
            yield
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # Companies
    # ------------------------------------------------------------------
    def upsert_company(self, company: CompanyTarget) -> None:
        now = utc_now_iso()
        with self._tx():
            self._conn.execute(
                """
                INSERT INTO companies(
                    source_key, ticker, exchange, name, isin, sec_cik,
                    hkex_stock_id, lei, ir_urls_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    ticker = excluded.ticker,
                    exchange = excluded.exchange,
                    name = excluded.name,
                    isin = excluded.isin,
                    sec_cik = excluded.sec_cik,
                    hkex_stock_id = excluded.hkex_stock_id,
                    lei = excluded.lei,
                    ir_urls_json = excluded.ir_urls_json,
                    updated_at = excluded.updated_at
                """,
                (
                    company.source_key,
                    company.ticker,
                    company.exchange,
                    company.name,
                    company.isin,
                    company.sec_cik,
                    company.hkex_stock_id,
                    company.lei,
                    json.dumps(company.ir_urls),
                    now,
                ),
            )

    def get_company(self, source_key: str) -> CompanyTarget | None:
        row = self._conn.execute(
            "SELECT * FROM companies WHERE source_key = ?", (source_key,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_company(row)

    def list_companies(self, allowed_source_keys: Collection[str]) -> list[CompanyTarget]:
        allowed = sorted(set(allowed_source_keys))
        if not allowed:
            return []
        placeholders = ",".join("?" for _ in allowed)
        rows = self._conn.execute(
            f"SELECT * FROM companies WHERE source_key IN ({placeholders}) ORDER BY source_key",
            allowed,
        ).fetchall()
        return [self._row_to_company(r) for r in rows]

    @staticmethod
    def _row_to_company(row: sqlite3.Row) -> CompanyTarget:
        return CompanyTarget(
            source_key=row["source_key"],
            ticker=row["ticker"],
            exchange=row["exchange"],
            name=row["name"],
            isin=row["isin"],
            sec_cik=row["sec_cik"],
            hkex_stock_id=row["hkex_stock_id"],
            lei=row["lei"],
            ir_urls=json.loads(row["ir_urls_json"] or "[]"),
        )

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------
    def upsert_document(self, doc: DocumentRecord) -> None:
        now = utc_now_iso()
        with self._tx():
            self._conn.execute(
                """
                INSERT INTO documents(
                    document_id, source_key, adapter, external_id, form_type, form_family,
                    title, filing_date, period_end, language, source_url, source_sha256,
                    source_mime, source_bytes, refetchable, is_amendment,
                    supersedes_document_id, converter_id, converter_version,
                    markdown_sha256, page_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key, adapter, external_id) DO UPDATE SET
                    form_type = excluded.form_type,
                    form_family = excluded.form_family,
                    title = excluded.title,
                    filing_date = excluded.filing_date,
                    period_end = excluded.period_end,
                    language = excluded.language,
                    source_url = excluded.source_url,
                    source_sha256 = excluded.source_sha256,
                    source_mime = excluded.source_mime,
                    source_bytes = excluded.source_bytes,
                    refetchable = excluded.refetchable,
                    is_amendment = excluded.is_amendment,
                    supersedes_document_id = excluded.supersedes_document_id,
                    converter_id = excluded.converter_id,
                    converter_version = excluded.converter_version,
                    markdown_sha256 = excluded.markdown_sha256,
                    page_count = excluded.page_count
                """,
                (
                    doc.document_id,
                    doc.source_key,
                    str(doc.adapter),
                    doc.external_id,
                    doc.form_type,
                    str(doc.form_family),
                    doc.title,
                    doc.filing_date,
                    doc.period_end,
                    doc.language,
                    doc.source_url,
                    doc.source_sha256,
                    doc.source_mime,
                    doc.source_bytes,
                    1 if doc.refetchable else 0,
                    1 if doc.is_amendment else 0,
                    doc.supersedes_document_id,
                    doc.converter_id,
                    doc.converter_version,
                    doc.markdown_sha256,
                    doc.page_count,
                    now,
                ),
            )

    def get_document(self, document_id: str) -> DocumentRecord | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        return self._row_to_document(row) if row is not None else None

    def get_document_by_external_id(
        self, source_key: str, adapter: SourceAdapter | str, external_id: str
    ) -> DocumentRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM documents
            WHERE source_key = ? AND adapter = ? AND external_id = ?
            """,
            (source_key, str(adapter), external_id),
        ).fetchone()
        return self._row_to_document(row) if row is not None else None

    def list_documents(
        self,
        allowed_source_keys: Collection[str],
        *,
        source_key: str | None = None,
        form_family: FormFamily | str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[DocumentRecord]:
        allowed = sorted(set(allowed_source_keys))
        if not allowed:
            return []
        if source_key is not None:
            if source_key not in allowed:
                return []
            allowed = [source_key]

        clauses = [f"source_key IN ({','.join('?' for _ in allowed)})"]
        params: list[Any] = list(allowed)
        if form_family is not None:
            clauses.append("form_family = ?")
            params.append(str(form_family))
        if since is not None:
            clauses.append("filing_date >= ?")
            params.append(since)
        bounded_limit = max(MIN_QUERY_LIMIT, min(int(limit), MAX_QUERY_LIMIT))
        params.append(bounded_limit)
        sql = (
            f"SELECT * FROM documents WHERE {' AND '.join(clauses)} "
            "ORDER BY filing_date DESC, document_id ASC LIMIT ?"
        )
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_document(r) for r in rows]

    def list_all_documents(self, allowed_source_keys: Collection[str]) -> list[DocumentRecord]:
        """List all documents for internal bulk operations such as Markdown export."""
        allowed = sorted(set(allowed_source_keys))
        if not allowed:
            return []
        placeholders = ",".join("?" for _ in allowed)
        rows = self._conn.execute(
            f"SELECT * FROM documents WHERE source_key IN ({placeholders}) "
            "ORDER BY filing_date DESC, document_id ASC",
            allowed,
        ).fetchall()
        return [self._row_to_document(r) for r in rows]

    def find_superseded_document_id(
        self,
        *,
        source_key: str,
        base_form_type: str,
        period_end: str | None,
        before_filing_date: str,
    ) -> str | None:
        """Find the latest non-amendment filing superseded by an amendment."""
        if period_end:
            row = self._conn.execute(
                """
                SELECT document_id FROM documents
                WHERE source_key = ?
                  AND form_type = ?
                  AND period_end = ?
                  AND is_amendment = 0
                  AND filing_date <= ?
                ORDER BY filing_date DESC
                LIMIT 1
                """,
                (source_key, base_form_type, period_end, before_filing_date),
            ).fetchone()
            return str(row["document_id"]) if row is not None else None
        row = self._conn.execute(
            """
            SELECT document_id FROM documents
            WHERE source_key = ?
              AND form_type = ?
              AND is_amendment = 0
              AND filing_date <= ?
            ORDER BY filing_date DESC
            LIMIT 1
            """,
            (source_key, base_form_type, before_filing_date),
        ).fetchone()
        return str(row["document_id"]) if row is not None else None

    def count_documents_for_issuer(self, source_key: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM documents WHERE source_key = ?", (source_key,)
        ).fetchone()
        return int(row["cnt"]) if row else 0

    def has_official_transcript(
        self,
        *,
        source_key: str,
        period_end: str | None = None,
        filing_date: str | None = None,
    ) -> bool:
        """Return True if an official (non-ASR) transcript exists for this issuer/period."""
        if period_end:
            row = self._conn.execute(
                """
                SELECT 1 FROM documents
                WHERE source_key = ?
                  AND form_family = 'transcript'
                  AND form_type != 'earnings_call_transcript_asr'
                  AND period_end = ?
                LIMIT 1
                """,
                (source_key, period_end),
            ).fetchone()
            if row is not None:
                return True
        if filing_date and filing_date != "undated":
            row = self._conn.execute(
                """
                SELECT 1 FROM documents
                WHERE source_key = ?
                  AND form_family = 'transcript'
                  AND form_type != 'earnings_call_transcript_asr'
                  AND filing_date = ?
                LIMIT 1
                """,
                (source_key, filing_date),
            ).fetchone()
            return row is not None
        return False

    def find_asr_transcript_to_supersede(
        self,
        *,
        source_key: str,
        period_end: str | None = None,
        filing_date: str | None = None,
    ) -> str | None:
        """Find a self-transcribed ASR transcript superseded by an official transcript."""
        if period_end:
            row = self._conn.execute(
                """
                SELECT document_id FROM documents
                WHERE source_key = ?
                  AND form_family = 'transcript'
                  AND form_type = 'earnings_call_transcript_asr'
                  AND period_end = ?
                ORDER BY filing_date DESC
                LIMIT 1
                """,
                (source_key, period_end),
            ).fetchone()
            if row is not None:
                return str(row["document_id"])
        if filing_date and filing_date != "undated":
            row = self._conn.execute(
                """
                SELECT document_id FROM documents
                WHERE source_key = ?
                  AND form_family = 'transcript'
                  AND form_type = 'earnings_call_transcript_asr'
                  AND filing_date = ?
                ORDER BY filing_date DESC
                LIMIT 1
                """,
                (source_key, filing_date),
            ).fetchone()
            return str(row["document_id"]) if row is not None else None
        return None

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> DocumentRecord:
        return DocumentRecord(
            document_id=row["document_id"],
            source_key=row["source_key"],
            adapter=SourceAdapter(row["adapter"]),
            external_id=row["external_id"],
            form_type=row["form_type"],
            form_family=FormFamily(row["form_family"]),
            title=row["title"],
            filing_date=row["filing_date"],
            period_end=row["period_end"],
            language=row["language"],
            source_url=row["source_url"],
            source_sha256=row["source_sha256"],
            source_mime=row["source_mime"],
            source_bytes=int(row["source_bytes"]),
            refetchable=bool(row["refetchable"]),
            is_amendment=bool(row["is_amendment"]),
            supersedes_document_id=row["supersedes_document_id"],
            converter_id=row["converter_id"],
            converter_version=row["converter_version"],
            markdown_sha256=row["markdown_sha256"],
            page_count=row["page_count"],
        )

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------
    def put_artifact(
        self,
        *,
        source_sha256: str,
        kind: ArtifactKind | str,
        converter_id: str,
        converter_version: str,
        artifact_sha256: str,
        byte_size: int,
    ) -> None:
        now = utc_now_iso()
        with self._tx():
            self._conn.execute(
                """
                INSERT OR REPLACE INTO artifacts(
                    source_sha256, kind, converter_id, converter_version,
                    artifact_sha256, byte_size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_sha256,
                    str(kind),
                    converter_id,
                    converter_version,
                    artifact_sha256,
                    byte_size,
                    now,
                ),
            )

    def get_artifact(
        self,
        *,
        source_sha256: str,
        kind: ArtifactKind | str,
        converter_id: str,
        converter_version: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT * FROM artifacts
            WHERE source_sha256 = ? AND kind = ? AND converter_id = ? AND converter_version = ?
            """,
            (source_sha256, str(kind), converter_id, converter_version),
        ).fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Sections & FTS5
    # ------------------------------------------------------------------
    def replace_sections(self, document_id: str, sections: Sequence[ExtractedSection]) -> None:
        with self._tx():
            doc = self.get_document(document_id)
            if doc is None:
                raise KeyError(f"Unknown document_id: {document_id}")
            self._conn.execute("DELETE FROM sections WHERE document_id = ?", (document_id,))
            self._conn.execute("DELETE FROM fts_chunks WHERE document_id = ?", (document_id,))
            for sec in sections:
                self._conn.execute(
                    """
                    INSERT INTO sections(document_id, locator, title, ordinal, markdown, ocr_used)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        sec.locator,
                        sec.title,
                        sec.ordinal,
                        sec.markdown,
                        1 if sec.ocr_used else 0,
                    ),
                )
                norm_nums = extract_normalized_numbers(f"{sec.title}\n{sec.markdown}")
                self._conn.execute(
                    """
                    INSERT INTO fts_chunks(
                        title, body, normalized_numbers, document_id, source_key, locator
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sec.title,
                        sec.markdown,
                        norm_nums,
                        document_id,
                        doc.source_key,
                        sec.locator,
                    ),
                )

    def get_sections(self, document_id: str) -> list[ExtractedSection]:
        rows = self._conn.execute(
            """
            SELECT locator, title, ordinal, markdown, ocr_used
            FROM sections
            WHERE document_id = ?
            ORDER BY ordinal ASC, id ASC
            """,
            (document_id,),
        ).fetchall()
        return [
            ExtractedSection(
                locator=r["locator"],
                title=r["title"],
                ordinal=int(r["ordinal"]),
                markdown=r["markdown"],
                ocr_used=bool(r["ocr_used"]),
            )
            for r in rows
        ]

    def rebuild_fts(self) -> int:
        """Atomically clear and deterministically repopulate `fts_chunks` from `sections`."""
        with self._tx():
            self._conn.execute("DELETE FROM fts_chunks;")
            rows = self._conn.execute(
                """
                SELECT s.document_id, d.source_key, s.locator, s.title, s.markdown
                FROM sections s
                JOIN documents d ON d.document_id = s.document_id
                ORDER BY s.document_id ASC, s.ordinal ASC, s.id ASC
                """
            ).fetchall()
            for r in rows:
                norm_nums = extract_normalized_numbers(f"{r['title']}\n{r['markdown']}")
                self._conn.execute(
                    """
                    INSERT INTO fts_chunks(
                        title, body, normalized_numbers, document_id, source_key, locator
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["title"],
                        r["markdown"],
                        norm_nums,
                        r["document_id"],
                        r["source_key"],
                        r["locator"],
                    ),
                )
        return len(rows)

    # ------------------------------------------------------------------
    # Company facts
    # ------------------------------------------------------------------
    def upsert_company_facts(self, source_key: str, cik: int, facts_sha256: str) -> None:
        now = utc_now_iso()
        with self._tx():
            self._conn.execute(
                """
                INSERT INTO company_facts(source_key, cik, facts_sha256, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    cik = excluded.cik,
                    facts_sha256 = excluded.facts_sha256,
                    fetched_at = excluded.fetched_at
                """,
                (source_key, cik, facts_sha256, now),
            )

    def get_company_facts(self, source_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM company_facts WHERE source_key = ?", (source_key,)
        ).fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Leased job queue with retry & dead-letter bounds
    # ------------------------------------------------------------------
    def enqueue_job(self, kind: str, payload: dict[str, Any], *, max_attempts: int = 3) -> int:
        now = utc_now_iso()
        with self._tx():
            cur = self._conn.execute(
                """
                INSERT INTO jobs(kind, payload_json, status, attempts, max_attempts, created_at, updated_at)
                VALUES (?, ?, 'pending', 0, ?, ?, ?)
                """,
                (kind, json.dumps(payload, sort_keys=True), max(1, max_attempts), now, now),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def enqueue_job_if_absent(
        self, kind: str, payload: dict[str, Any], *, max_attempts: int = 3
    ) -> int | None:
        """Enqueue a pending/leased job only if no active job with identical kind+payload exists."""
        payload_json = json.dumps(payload, sort_keys=True)
        now = utc_now_iso()
        with self._tx():
            existing = self._conn.execute(
                """
                SELECT id FROM jobs
                WHERE kind = ? AND payload_json = ? AND status IN ('pending', 'leased')
                LIMIT 1
                """,
                (kind, payload_json),
            ).fetchone()
            if existing is not None:
                return None
            cur = self._conn.execute(
                """
                INSERT INTO jobs(kind, payload_json, status, attempts, max_attempts, created_at, updated_at)
                VALUES (?, ?, 'pending', 0, ?, ?, ?)
                """,
                (kind, payload_json, max(1, max_attempts), now, now),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def lease_job(
        self, owner: str, *, lease_seconds: int = 300, now: datetime | None = None
    ) -> dict[str, Any] | None:
        current = now or datetime.now(tz=UTC)
        now_iso = current.strftime("%Y-%m-%dT%H:%M:%SZ")
        expires_iso = (current + timedelta(seconds=lease_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._tx():
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'dead',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = COALESCE(last_error, 'lease expired at max_attempts'),
                    updated_at = ?
                WHERE status = 'leased'
                  AND lease_expires_at <= ?
                  AND attempts >= max_attempts
                """,
                (now_iso, now_iso),
            )
            row = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE (
                    status = 'pending'
                    OR (status = 'leased' AND lease_expires_at <= ?)
                )
                  AND attempts < max_attempts
                ORDER BY id ASC
                LIMIT 1
                """,
                (now_iso,),
            ).fetchone()
            if row is None:
                return None
            new_attempts = int(row["attempts"]) + 1
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'leased',
                    attempts = ?,
                    lease_owner = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (new_attempts, owner, expires_iso, now_iso, row["id"]),
            )
            updated = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            assert updated is not None
            result = dict(updated)
            result["payload"] = json.loads(result["payload_json"])
            return result

    def complete_job(self, job_id: int) -> None:
        now = utc_now_iso()
        with self._tx():
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'done', lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )

    def fail_job(self, job_id: int, error: str) -> str:
        """Record a job failure; returns `'pending'` if retryable or `'dead'` if exhausted."""
        now = utc_now_iso()
        with self._tx():
            row = self._conn.execute(
                "SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown job_id: {job_id}")
            next_status = "dead" if int(row["attempts"]) >= int(row["max_attempts"]) else "pending"
            self._conn.execute(
                """
                UPDATE jobs
                SET status = ?, last_error = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (next_status, error[:2000], now, job_id),
            )
            return next_status

    # ------------------------------------------------------------------
    # Storage usage & quotas
    # ------------------------------------------------------------------
    def storage_usage(self, source_key: str | None = None) -> dict[str, int]:
        """Return tracked raw + artifact bytes globally or for a specific issuer."""
        if source_key is None:
            raw_row = self._conn.execute(
                "SELECT COALESCE(SUM(source_bytes), 0) AS total FROM documents"
            ).fetchone()
            art_row = self._conn.execute(
                "SELECT COALESCE(SUM(byte_size), 0) AS total FROM artifacts"
            ).fetchone()
        else:
            raw_row = self._conn.execute(
                "SELECT COALESCE(SUM(source_bytes), 0) AS total FROM documents WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            art_row = self._conn.execute(
                """
                SELECT COALESCE(SUM(a.byte_size), 0) AS total
                FROM artifacts a
                WHERE a.source_sha256 IN (
                    SELECT DISTINCT source_sha256 FROM documents WHERE source_key = ?
                )
                """,
                (source_key,),
            ).fetchone()
        raw_bytes = int(raw_row["total"]) if raw_row else 0
        artifact_bytes = int(art_row["total"]) if art_row else 0
        return {
            "raw_bytes": raw_bytes,
            "artifact_bytes": artifact_bytes,
            "total_bytes": raw_bytes + artifact_bytes,
        }

    def check_quota(
        self,
        additional_bytes: int,
        *,
        source_key: str | None = None,
        global_cap_bytes: int = DEFAULT_GLOBAL_CAP_BYTES,
        per_issuer_cap_bytes: int | None = None,
        cas_bytes: int | None = None,
    ) -> QuotaStatus:
        """Check whether `additional_bytes` fits within global and per-issuer caps."""
        global_usage = self.storage_usage()["total_bytes"]
        effective_global = max(global_usage, cas_bytes) if cas_bytes is not None else global_usage
        issuer_usage = self.storage_usage(source_key)["total_bytes"] if source_key else 0

        if effective_global + additional_bytes > global_cap_bytes:
            return QuotaStatus(
                allowed=False,
                global_used_bytes=effective_global,
                global_cap_bytes=global_cap_bytes,
                issuer_used_bytes=issuer_usage,
                issuer_cap_bytes=per_issuer_cap_bytes,
                reason=(
                    f"Global storage cap exceeded: {effective_global + additional_bytes} "
                    f"> {global_cap_bytes}"
                ),
            )
        if (
            source_key is not None
            and per_issuer_cap_bytes is not None
            and issuer_usage + additional_bytes > per_issuer_cap_bytes
        ):
            return QuotaStatus(
                allowed=False,
                global_used_bytes=effective_global,
                global_cap_bytes=global_cap_bytes,
                issuer_used_bytes=issuer_usage,
                issuer_cap_bytes=per_issuer_cap_bytes,
                reason=(
                    f"Per-issuer storage cap exceeded for {source_key}: "
                    f"{issuer_usage + additional_bytes} > {per_issuer_cap_bytes}"
                ),
            )
        return QuotaStatus(
            allowed=True,
            global_used_bytes=effective_global,
            global_cap_bytes=global_cap_bytes,
            issuer_used_bytes=issuer_usage,
            issuer_cap_bytes=per_issuer_cap_bytes,
        )

    def assert_quota(
        self,
        additional_bytes: int,
        *,
        source_key: str | None = None,
        global_cap_bytes: int = DEFAULT_GLOBAL_CAP_BYTES,
        per_issuer_cap_bytes: int | None = None,
        cas_bytes: int | None = None,
    ) -> QuotaStatus:
        status = self.check_quota(
            additional_bytes,
            source_key=source_key,
            global_cap_bytes=global_cap_bytes,
            per_issuer_cap_bytes=per_issuer_cap_bytes,
            cas_bytes=cas_bytes,
        )
        if not status.allowed:
            raise QuotaExceededError(status.reason or "Storage quota exceeded")
        return status
