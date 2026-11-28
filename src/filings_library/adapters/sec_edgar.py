"""SEC EDGAR submissions, 6-K EX-99 exhibit selection, and XBRL companyfacts adapter."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from filings_library.adapters import FilingCandidate
from filings_library.models import CompanyTarget, FormFamily, SourceAdapter
from filings_library.url_safety import (
    SEC_ALLOWED_HOSTS,
    HostResolver,
    UnsafeUrlError,
    validate_public_https_url,
)

if TYPE_CHECKING:
    from filings_library.store import FilingsStore

SEC_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
SEC_SUBMISSIONS_URL = f"{SEC_SUBMISSIONS_BASE}/CIK{{cik:010d}}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
MAX_OVERFLOW_FILES = 10

_ALLOWED_FORMS = frozenset(
    {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "6-K", "8-K", "DEF 14A"}
)
_ALLOWED_8K_ITEMS = frozenset({"2.02", "7.01", "8.01"})

_FORM_FAMILY_MAP: dict[str, FormFamily] = {
    "10-K": FormFamily.ANNUAL_REPORT,
    "10-K/A": FormFamily.ANNUAL_REPORT,
    "20-F": FormFamily.ANNUAL_REPORT,
    "20-F/A": FormFamily.ANNUAL_REPORT,
    "10-Q": FormFamily.QUARTERLY_REPORT,
    "10-Q/A": FormFamily.QUARTERLY_REPORT,
    "6-K": FormFamily.RESULTS_RELEASE,
    "8-K": FormFamily.CURRENT_REPORT,
    "DEF 14A": FormFamily.PROXY,
}

_EX99_NAME_RE = re.compile(r"(?:ex(?:hibit)?[-_]?99|99[-_.]?\d)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

_ROUTINE_6K_SUBSTRINGS = (
    "buyback",
    "buy-back",
    "buy back",
    "own shares",
    "transaction in own shares",
    "total voting rights",
    "pdmr",
    "block listing",
    "director",
    "holding(s) in company",
    "holdings in company",
    "holding in company",
)


def is_routine_6k(title: str, doc_name: str = "") -> bool:
    """Return True when a 6-K filing is routine share/governance noise, not results."""
    raw = f"{title or ''} {doc_name or ''}".lower()
    normalized = re.sub(r"[_-]+", " ", raw)
    return any(marker in raw or marker in normalized for marker in _ROUTINE_6K_SUBSTRINGS)


def validate_sec_user_agent(user_agent: str) -> str:
    """Enforce SEC EDGAR fair-access User-Agent requirement (non-empty with contact email)."""
    cleaned = (user_agent or "").strip()
    if not cleaned or not _EMAIL_RE.search(cleaned):
        raise ValueError(
            "SEC EDGAR requires an explicit User-Agent containing a contact email "
            "(e.g. 'FilingsLibrary/0.1 (contact@example.com)')"
        )
    return cleaned


def _guess_mime(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return "text/html"


def _is_safe_doc_name(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        return False
    return True


def _select_ex99_exhibit(index_json: dict[str, Any], primary_doc: str) -> str | None:
    """Pick the best EX-99.1 / EX-99.* exhibit from an accession `index.json`, or None."""
    items = index_json.get("directory", {}).get("item", [])
    if not isinstance(items, list):
        return None

    candidates: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not _is_safe_doc_name(name):
            continue
        lower = name.lower()
        if not lower.endswith((".htm", ".html", ".pdf")):
            continue
        if lower == primary_doc.lower() or "index" in lower:
            continue
        if _EX99_NAME_RE.search(name):
            # Prefer 99-1 / 99_1 / ex991 HTML over secondary exhibits or PDFs.
            score = 0
            if re.search(r"99[-_.]?1\b", lower):
                score -= 20
            if lower.endswith((".htm", ".html")):
                score -= 10
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            candidates.append((score - (1 if size > 4096 else 0), name))

    if candidates:
        candidates.sort(key=lambda pair: (pair[0], pair[1]))
        return candidates[0][1]
    return None


def _select_6k_exhibit(index_json: dict[str, Any], primary_doc: str) -> str:
    """Pick an earnings/results EX-99.* exhibit from a 6-K accession `index.json`."""
    return _select_ex99_exhibit(index_json, primary_doc) or primary_doc


class SecEdgarAdapter:
    """Tier-1 SEC EDGAR adapter for submissions discovery and XBRL companyfacts."""

    def __init__(
        self,
        *,
        user_agent: str,
        backfill_years: int = 3,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: HostResolver | None = None,
        store: FilingsStore | None = None,
    ) -> None:
        self._user_agent = validate_sec_user_agent(user_agent)
        self._backfill_years = max(1, backfill_years)
        self._timeout = timeout
        self._transport = transport
        self._resolver = resolver
        self._store = store
        self._index_cache: dict[tuple[int, str], dict[str, Any]] = {}

    async def _fetch_accession_index(
        self,
        client: httpx.AsyncClient,
        *,
        cik: int,
        accn: str,
        accn_nodash: str,
    ) -> dict[str, Any] | None:
        cache_key = (cik, accn)
        if cache_key in self._index_cache:
            return self._index_cache[cache_key]
        if self._store is not None:
            cached = self._store.get_sec_index_cache(cik, accn)
            if cached is not None:
                self._index_cache[cache_key] = cached
                return cached

        index_url = validate_public_https_url(
            f"{SEC_ARCHIVES_BASE}/{cik}/{accn_nodash}/index.json",
            allowed_hosts=SEC_ALLOWED_HOSTS,
            resolver=self._resolver,
        )
        idx_resp = await client.get(index_url)
        if idx_resp.status_code != 200:
            return None
        data = idx_resp.json()
        if not isinstance(data, dict):
            return None
        self._index_cache[cache_key] = data
        if self._store is not None:
            self._store.put_sec_index_cache(cik, accn, data)
        return data

    @property
    def user_agent(self) -> str:
        return self._user_agent

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._user_agent, "Accept": "application/json"}

    async def discover(
        self, company: CompanyTarget, *, today: date | None = None
    ) -> list[FilingCandidate]:
        if company.sec_cik is None:
            return []

        cik = int(company.sec_cik)
        ref_date = today or date.today()
        cutoff = (ref_date - timedelta(days=365 * self._backfill_years)).isoformat()

        submissions_url = validate_public_https_url(
            SEC_SUBMISSIONS_URL.format(cik=cik),
            allowed_hosts=SEC_ALLOWED_HOSTS,
            resolver=self._resolver,
        )

        async with httpx.AsyncClient(
            headers=self._headers(),
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            resp = await client.get(submissions_url)
            resp.raise_for_status()
            payload = resp.json()

            filings_obj = payload.get("filings", {})
            recent = filings_obj.get("recent", {})
            blocks: list[dict[str, Any]] = [recent] if isinstance(recent, dict) else []

            recent_dates = [
                str(d).strip()
                for d in (recent.get("filingDate", []) if isinstance(recent, dict) else [])
                if str(d).strip()
            ]
            overflow_files = filings_obj.get("files", []) if isinstance(filings_obj, dict) else []
            if isinstance(overflow_files, list) and (
                not recent_dates or min(recent_dates) >= cutoff
            ):
                fetched_overflow = 0
                for file_meta in overflow_files:
                    if fetched_overflow >= MAX_OVERFLOW_FILES:
                        break
                    if not isinstance(file_meta, dict):
                        continue
                    filing_to = str(file_meta.get("filingTo") or "").strip()
                    if filing_to and filing_to < cutoff:
                        continue
                    file_name = str(file_meta.get("name") or "").strip()
                    if not _is_safe_doc_name(file_name) or not file_name.endswith(".json"):
                        continue
                    try:
                        overflow_url = validate_public_https_url(
                            f"{SEC_SUBMISSIONS_BASE}/{file_name}",
                            allowed_hosts=SEC_ALLOWED_HOSTS,
                            resolver=self._resolver,
                        )
                    except UnsafeUrlError:
                        continue
                    overflow_resp = await client.get(overflow_url)
                    if overflow_resp.status_code == 200:
                        overflow_data = overflow_resp.json()
                        if isinstance(overflow_data, dict):
                            blocks.append(overflow_data)
                            fetched_overflow += 1

            candidates: list[FilingCandidate] = []
            seen_accns: set[str] = set()
            for block in blocks:
                forms: list[str] = block.get("form", [])
                accession_numbers: list[str] = block.get("accessionNumber", [])
                filing_dates: list[str] = block.get("filingDate", [])
                report_dates: list[str] = block.get("reportDate", [])
                primary_docs: list[str] = block.get("primaryDocument", [])
                primary_descs: list[str] = block.get("primaryDocDescription", [])
                items_list: list[Any] = block.get("items", [])

                count = min(
                    len(forms), len(accession_numbers), len(filing_dates), len(primary_docs)
                )
                for idx in range(count):
                    form = str(forms[idx]).strip().upper()
                    if form not in _ALLOWED_FORMS:
                        continue
                    fdate = str(filing_dates[idx]).strip()
                    if not fdate or fdate < cutoff:
                        continue

                    item_set: set[str] = set()
                    if form == "8-K":
                        raw_items = items_list[idx] if idx < len(items_list) else []
                        if isinstance(raw_items, str):
                            item_set = {s.strip() for s in raw_items.split(",") if s.strip()}
                        elif isinstance(raw_items, list):
                            item_set = {str(s).strip() for s in raw_items}
                        if not (item_set & _ALLOWED_8K_ITEMS):
                            continue

                    accn = str(accession_numbers[idx]).strip()
                    if accn in seen_accns:
                        continue
                    accn_nodash = accn.replace("-", "")
                    if not accn_nodash or not accn_nodash.isdigit():
                        continue
                    primary_doc = str(primary_docs[idx]).strip()
                    selected_doc = primary_doc
                    ex99_doc: str | None = None

                    if form == "6-K":
                        index_data = await self._fetch_accession_index(
                            client, cik=cik, accn=accn, accn_nodash=accn_nodash
                        )
                        if index_data is not None:
                            selected_doc = _select_6k_exhibit(index_data, primary_doc)
                    elif form == "8-K" and "2.02" in item_set:
                        index_data = await self._fetch_accession_index(
                            client, cik=cik, accn=accn, accn_nodash=accn_nodash
                        )
                        if index_data is not None:
                            ex99_doc = _select_ex99_exhibit(index_data, primary_doc)

                    if not _is_safe_doc_name(selected_doc):
                        continue

                    period_end = (
                        str(report_dates[idx]).strip() or None if idx < len(report_dates) else None
                    )
                    desc = (
                        str(primary_descs[idx]).strip()
                        if idx < len(primary_descs) and primary_descs[idx]
                        else f"{company.name} {form}"
                    )
                    is_amendment = form.endswith("/A")
                    base_form = form[:-2] if is_amendment else form
                    form_family = _FORM_FAMILY_MAP[form]
                    if form == "6-K" and is_routine_6k(desc, selected_doc):
                        form_family = FormFamily.CURRENT_REPORT

                    try:
                        source_url = validate_public_https_url(
                            f"{SEC_ARCHIVES_BASE}/{cik}/{accn_nodash}/{selected_doc}",
                            allowed_hosts=SEC_ALLOWED_HOSTS,
                            resolver=self._resolver,
                        )
                    except UnsafeUrlError:
                        continue

                    seen_accns.add(accn)
                    candidates.append(
                        FilingCandidate(
                            source_key=company.source_key,
                            adapter=SourceAdapter.SEC_EDGAR,
                            external_id=f"{accn}:{selected_doc}",
                            form_type=form,
                            form_family=form_family,
                            title=desc,
                            filing_date=fdate,
                            period_end=period_end,
                            source_url=source_url,
                            source_mime=_guess_mime(selected_doc),
                            language="en",
                            refetchable=True,
                            is_amendment=is_amendment,
                            base_form_type=base_form,
                        )
                    )

                    if ex99_doc and _is_safe_doc_name(ex99_doc) and ex99_doc != selected_doc:
                        try:
                            ex99_url = validate_public_https_url(
                                f"{SEC_ARCHIVES_BASE}/{cik}/{accn_nodash}/{ex99_doc}",
                                allowed_hosts=SEC_ALLOWED_HOSTS,
                                resolver=self._resolver,
                            )
                        except UnsafeUrlError:
                            ex99_url = ""
                        if ex99_url:
                            candidates.append(
                                FilingCandidate(
                                    source_key=company.source_key,
                                    adapter=SourceAdapter.SEC_EDGAR,
                                    external_id=f"{accn}:{ex99_doc}",
                                    form_type="8-K EX-99.1",
                                    form_family=FormFamily.RESULTS_RELEASE,
                                    title=f"{desc} (Exhibit 99.1)",
                                    filing_date=fdate,
                                    period_end=period_end,
                                    source_url=ex99_url,
                                    source_mime=_guess_mime(ex99_doc),
                                    language="en",
                                    refetchable=True,
                                    is_amendment=is_amendment,
                                    base_form_type="8-K",
                                )
                            )

        return candidates

    async def fetch_company_facts(self, cik: int) -> dict[str, Any]:
        """Fetch SEC XBRL `companyfacts` JSON for `cik`."""
        facts_url = validate_public_https_url(
            SEC_COMPANYFACTS_URL.format(cik=int(cik)),
            allowed_hosts=SEC_ALLOWED_HOSTS,
            resolver=self._resolver,
        )
        async with httpx.AsyncClient(
            headers=self._headers(),
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            resp = await client.get(facts_url)
            resp.raise_for_status()
            return dict(resp.json())


def query_facts(
    facts_json: dict[str, Any],
    concepts: Sequence[str],
    periods: Sequence[str] | None = None,
    *,
    taxonomy: str = "us-gaap",
) -> list[dict[str, Any]]:
    """Query structured numeric facts from SEC XBRL `companyfacts` JSON."""
    facts_root = facts_json.get("facts", {})
    if not isinstance(facts_root, dict):
        return []

    period_filters = {p.strip().upper() for p in periods} if periods else None
    taxonomy_order = [taxonomy] + [t for t in facts_root.keys() if t != taxonomy]
    results: list[dict[str, Any]] = []

    for concept in concepts:
        matched_tax: str | None = None
        concept_obj: dict[str, Any] | None = None
        for tax in taxonomy_order:
            tax_dict = facts_root.get(tax)
            if isinstance(tax_dict, dict) and concept in tax_dict:
                matched_tax = tax
                concept_obj = tax_dict[concept]
                break
        if concept_obj is None or matched_tax is None:
            continue

        units = concept_obj.get("units", {})
        if not isinstance(units, dict):
            continue

        for unit_name, entries in units.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                fy = entry.get("fy")
                fp = str(entry.get("fp") or "").upper()
                end = str(entry.get("end") or "")
                if period_filters is not None:
                    candidates_keys = {end.upper()}
                    if fy is not None:
                        fy_s = str(fy)
                        candidates_keys.add(fy_s)
                        candidates_keys.add(f"FY{fy_s}")
                        if fp:
                            candidates_keys.add(f"{fy_s}-{fp}")
                            candidates_keys.add(f"{fp}{fy_s}")
                            candidates_keys.add(f"{fp}-{fy_s}")
                    if not (candidates_keys & period_filters):
                        continue
                results.append(
                    {
                        "concept": concept,
                        "taxonomy": matched_tax,
                        "unit": unit_name,
                        "val": entry.get("val"),
                        "start": entry.get("start"),
                        "end": end,
                        "fy": fy,
                        "fp": fp,
                        "form": entry.get("form"),
                        "filed": entry.get("filed"),
                        "accn": entry.get("accn"),
                    }
                )

    results.sort(key=lambda r: (str(r.get("end") or ""), str(r.get("filed") or "")), reverse=True)
    return results
