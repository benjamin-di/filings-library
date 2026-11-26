"""Scoped filings service layer shared by FastMCP server and CLI."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any, Protocol, TypeVar

from filings_library.adapters.sec_edgar import query_facts
from filings_library.cas import ContentAddressedStore
from filings_library.models import FormFamily
from filings_library.search import read_document, search_filings
from filings_library.store import FilingsStore
from filings_library.targets_client import LibraryTargetsClient

T = TypeVar("T")

MIN_LIMIT = 1
MAX_LIMIT = 200
MIN_MAX_CHARS = 100
MAX_MAX_CHARS = 200_000
MIN_LIMIT_PER_COMPANY = 1
MAX_LIMIT_PER_COMPANY = 50


def clamp_limit(value: int) -> int:
    return max(MIN_LIMIT, min(int(value), MAX_LIMIT))


def clamp_max_chars(value: int) -> int:
    return max(MIN_MAX_CHARS, min(int(value), MAX_MAX_CHARS))


def clamp_limit_per_company(value: int) -> int:
    return max(MIN_LIMIT_PER_COMPANY, min(int(value), MAX_LIMIT_PER_COMPANY))


class ScopeResolver(Protocol):
    """Resolves the allowed `source_keys` for an authenticated subject."""

    async def allowed_source_keys(self, person: str) -> Collection[str]: ...


class TargetsScopeResolver:
    """Fail-closed scope resolver backed by `LibraryTargetsClient`."""

    def __init__(self, client: LibraryTargetsClient) -> None:
        self._client = client

    async def allowed_source_keys(self, person: str) -> set[str]:
        return await self._client.allowed_source_keys_for(person)


class StaticScopeResolver:
    """In-memory scope resolver for tests or explicit static grants."""

    def __init__(self, scopes: Mapping[str, Collection[str]]) -> None:
        self._scopes = {person: set(keys) for person, keys in scopes.items()}

    async def allowed_source_keys(self, person: str) -> set[str]:
        return set(self._scopes.get(person, set()))


class AllCompaniesScopeResolver:
    """Local operator scope resolver for CLI `--all` mode."""

    def __init__(self, store: FilingsStore) -> None:
        self._store = store

    async def allowed_source_keys(self, person: str) -> set[str]:
        del person
        rows = self._store.conn.execute("SELECT source_key FROM companies").fetchall()
        return {str(r["source_key"]) for r in rows}


class FilingsService:
    """Core multi-user scoped operations over SQLite store and SHA-256 CAS.

    Every operation resolves `allowed_source_keys(person)` through `scope_resolver`
    and fails closed if the upstream hub raises `HubUnavailableError`.
    """

    def __init__(
        self,
        store: FilingsStore,
        cas: ContentAddressedStore,
        scope_resolver: ScopeResolver,
    ) -> None:
        self._store = store
        self._cas = cas
        self._scope_resolver = scope_resolver
        self._db_lock = threading.Lock()

    @property
    def store(self) -> FilingsStore:
        return self._store

    @property
    def cas(self) -> ContentAddressedStore:
        return self._cas

    async def _run_db(self, fn: Callable[[], T]) -> T:
        def _locked() -> T:
            with self._db_lock:
                return fn()

        return await asyncio.to_thread(_locked)

    async def _allowed_keys(self, person: str) -> set[str]:
        keys = await self._scope_resolver.allowed_source_keys(person)
        return set(keys)

    async def health_check(self) -> bool:
        """Verify SQLite liveness via a lightweight `SELECT 1` off the event loop."""

        def _check() -> bool:
            try:
                row = self._store.conn.execute("SELECT 1;").fetchone()
                return row is not None and int(row[0]) == 1
            except Exception:
                return False

        return await self._run_db(_check)

    async def list_companies(self, person: str) -> list[dict[str, Any]]:
        """Return only companies in `person`'s allowed `source_keys`."""
        allowed = await self._allowed_keys(person)

        def _query() -> list[dict[str, Any]]:
            companies = self._store.list_companies(allowed)
            return [c.model_dump() for c in companies]

        return await self._run_db(_query)

    async def list_documents(
        self,
        person: str,
        *,
        source_key: str | None = None,
        form_family: FormFamily | str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List documents strictly within `person`'s allowed `source_keys`."""
        allowed = await self._allowed_keys(person)
        bounded_limit = clamp_limit(limit)

        def _query() -> list[dict[str, Any]]:
            docs = self._store.list_documents(
                allowed,
                source_key=source_key,
                form_family=form_family,
                since=since,
                limit=bounded_limit,
            )
            return [d.model_dump() for d in docs]

        return await self._run_db(_query)

    async def search(
        self,
        person: str,
        query: str,
        *,
        source_key: str | None = None,
        form_family: FormFamily | str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search filings strictly within `person`'s allowed `source_keys`."""
        allowed = await self._allowed_keys(person)
        bounded_limit = clamp_limit(limit)

        def _query() -> list[dict[str, Any]]:
            citations = search_filings(
                self._store,
                query,
                allowed_source_keys=allowed,
                source_key=source_key,
                form_family=form_family,
                since=since,
                limit=bounded_limit,
            )
            return [c.model_dump() for c in citations]

        return await self._run_db(_query)

    async def read(
        self,
        person: str,
        document_id: str,
        *,
        start_locator: str | None = None,
        end_locator: str | None = None,
        max_chars: int = 24000,
    ) -> dict[str, Any]:
        """Read bounded sections of a document within `person`'s allowed scope."""
        allowed = await self._allowed_keys(person)
        bounded_max_chars = clamp_max_chars(max_chars)

        def _query() -> dict[str, Any]:
            return read_document(
                self._store,
                document_id=document_id,
                allowed_source_keys=allowed,
                start_locator=start_locator,
                end_locator=end_locator,
                max_chars=bounded_max_chars,
            )

        return await self._run_db(_query)

    async def facts(
        self,
        person: str,
        concepts: Sequence[str],
        *,
        source_keys: Sequence[str] | None = None,
        periods: Sequence[str] | None = None,
        limit_per_company: int = 8,
    ) -> list[dict[str, Any]]:
        """Query cached SEC XBRL `companyfacts` across `person`'s allowed companies."""
        allowed = await self._allowed_keys(person)
        if not concepts:
            return []
        bounded_limit = clamp_limit_per_company(limit_per_company)

        if source_keys is not None:
            target_keys = sorted(k for k in set(source_keys) if k in allowed)
        else:
            target_keys = sorted(allowed)

        if not target_keys:
            return []

        def _query() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for sk in target_keys:
                meta = self._store.get_company_facts(sk)
                if meta is None:
                    continue
                facts_sha = str(meta["facts_sha256"])
                if not self._cas.has(facts_sha):
                    continue
                raw_bytes = self._cas.get_bytes(facts_sha)
                facts_json = json.loads(raw_bytes.decode("utf-8"))
                company = self._store.get_company(sk)
                matched = query_facts(facts_json, concepts, periods=periods)
                for row in matched[:bounded_limit]:
                    enriched = dict(row)
                    enriched["source_key"] = sk
                    enriched["ticker"] = company.ticker if company else ""
                    enriched["company_name"] = company.name if company else ""
                    enriched["cik"] = int(meta["cik"])
                    results.append(enriched)
            return results

        return await self._run_db(_query)

    async def status(self, person: str) -> dict[str, Any]:
        """Return per-issuer coverage report strictly for `person`'s allowed companies.

        Never exposes corpus-wide totals or unfollowed issuer state.
        """
        allowed = await self._allowed_keys(person)

        def _query() -> dict[str, Any]:
            companies = self._store.list_companies(allowed)
            issuer_reports: list[dict[str, Any]] = []
            for company in companies:
                sk = company.source_key
                family_rows = self._store.conn.execute(
                    """
                    SELECT form_family, COUNT(*) AS cnt
                    FROM documents
                    WHERE source_key = ?
                    GROUP BY form_family
                    ORDER BY form_family ASC
                    """,
                    (sk,),
                ).fetchall()
                counts_by_family = {str(r["form_family"]): int(r["cnt"]) for r in family_rows}
                doc_count = sum(counts_by_family.values())

                adapter_rows = self._store.conn.execute(
                    """
                    SELECT DISTINCT adapter
                    FROM documents
                    WHERE source_key = ?
                    ORDER BY adapter ASC
                    """,
                    (sk,),
                ).fetchall()
                adapters = [str(r["adapter"]) for r in adapter_rows]

                latest_row = self._store.conn.execute(
                    "SELECT MAX(filing_date) AS latest FROM documents WHERE source_key = ?",
                    (sk,),
                ).fetchone()
                latest_filing_date = (
                    str(latest_row["latest"])
                    if latest_row is not None and latest_row["latest"] is not None
                    else None
                )

                has_facts = self._store.get_company_facts(sk) is not None

                issuer_reports.append(
                    {
                        "source_key": sk,
                        "ticker": company.ticker,
                        "exchange": company.exchange,
                        "name": company.name,
                        "document_count": doc_count,
                        "counts_by_form_family": counts_by_family,
                        "adapters": adapters,
                        "latest_filing_date": latest_filing_date,
                        "has_company_facts": has_facts,
                    }
                )

            return {
                "person": person,
                "company_count": len(issuer_reports),
                "companies": issuer_reports,
            }

        return await self._run_db(_query)
