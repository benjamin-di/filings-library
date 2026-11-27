"""Read-only Web Workspace JSON API, formatting helpers, redline diff, and XBRL mapper.

Kept strictly read-only and scoped per authenticated subject via `FilingsService`.
Zero write/upload endpoints; raw HTML responses are strictly sandboxed via CSP.
"""

from __future__ import annotations

import difflib
import hashlib
import html
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from signin_window.connections import Suite
from signin_window.connections import page as connection_page
from signin_window.oidc import PAGE_HEADERS, BrowserAuth
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from filings_library.models import DocumentRecord, ExtractedSection
from filings_library.service import FilingsService, clamp_limit
from filings_library.targets_client import HubUnavailableError

WEB_STATIC_DIR = Path(__file__).resolve().parent / "web_static"

# Chunk threshold for very large single-section documents (e.g. 100+ MB ESEF annual reports).
LARGE_SECTION_CHUNK_THRESHOLD = 48_000
TARGET_CHUNK_CHARS = 32_000

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HEADING_LINE_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)
_HKEX_BRACKET_RE = re.compile(r"\[([^\]]+)\]")
_WORD_TOKEN_RE = re.compile(r"\S+\s*")

# Standardized US-GAAP + IFRS XBRL financial statement lines (ordered for analyst display).
STANDARD_XBRL_METRICS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "revenue",
        "Revenue",
        "income",
        (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            "Revenue",
        ),
    ),
    (
        "gross_profit",
        "Gross Profit",
        "income",
        ("GrossProfit",),
    ),
    (
        "operating_income",
        "Operating Income",
        "income",
        ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities"),
    ),
    (
        "net_income",
        "Net Income",
        "income",
        ("NetIncomeLoss", "ProfitLoss", "ProfitLossAttributableToOwnersOfParent"),
    ),
    (
        "eps_diluted",
        "Diluted EPS",
        "income",
        ("EarningsPerShareDiluted", "DilutedEarningsLossPerShare"),
    ),
    (
        "operating_cash_flow",
        "Operating Cash Flow",
        "cashflow",
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "CashFlowsFromUsedInOperatingActivities",
        ),
    ),
    (
        "capex",
        "Capital Expenditures",
        "cashflow",
        (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        ),
    ),
    (
        "cash",
        "Cash & Equivalents",
        "balance",
        (
            "CashAndCashEquivalentsAtCarryingValue",
            "CashAndCashEquivalents",
        ),
    ),
    (
        "total_assets",
        "Total Assets",
        "balance",
        ("Assets",),
    ),
    (
        "total_liabilities",
        "Total Liabilities",
        "balance",
        ("Liabilities",),
    ),
    (
        "stockholders_equity",
        "Stockholders' Equity",
        "balance",
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "Equity",
        ),
    ),
)

PAGE_SECURITY_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "strict-origin",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
}

API_SECURITY_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def build_viewer_url(
    public_base_url: str | None,
    document_id: str,
    *,
    locator: str | None = None,
    query: str | None = None,
) -> str | None:
    """Return a canonical deep-link URL into the web workspace when configured."""
    if not public_base_url:
        return None
    base = public_base_url.rstrip("/")
    params: dict[str, str] = {}
    if locator:
        params["loc"] = locator
    if query:
        params["q"] = query
    qs = f"?{urlencode(params)}" if params else ""
    return f"{base}/d/{quote(document_id, safe='')}{qs}"


def format_display_form_type(form_type: str, adapter: str = "") -> str:
    """Clean raw HTML-entity-encoded HKEX categories into crisp UI badges."""
    cleaned = _WS_RE.sub(" ", _HTML_TAG_RE.sub(" ", html.unescape(form_type or ""))).strip()
    if not cleaned:
        return "Filing"
    if adapter == "hkexnews" or len(cleaned) > 36:
        bracket_match = _HKEX_BRACKET_RE.search(cleaned)
        if bracket_match:
            inner = bracket_match.group(1).strip()
            # Take the first concise category token before '/' if overly verbose.
            primary = inner.split("/")[0].strip()
            if primary:
                return primary[:32]
        if len(cleaned) > 32:
            return cleaned[:29].rstrip() + "..."
    return cleaned


@dataclass(frozen=True)
class VirtualSection:
    locator: str
    title: str
    ordinal: int
    markdown: str
    ocr_used: bool
    parent_locator: str


def expand_sections_for_reader(sections: Sequence[ExtractedSection]) -> list[VirtualSection]:
    """Split oversized single-section docs (e.g. 1–2.4 MB ESEF HTML) into navigable chunks."""
    expanded: list[VirtualSection] = []
    ordinal = 0
    for sec in sections:
        md = sec.markdown or ""
        if len(md) <= LARGE_SECTION_CHUNK_THRESHOLD:
            expanded.append(
                VirtualSection(
                    locator=sec.locator,
                    title=sec.title or sec.locator,
                    ordinal=ordinal,
                    markdown=md,
                    ocr_used=sec.ocr_used,
                    parent_locator=sec.locator,
                )
            )
            ordinal += 1
            continue

        # Try splitting on Markdown headings first; fall back to fixed paragraph chunks.
        headings = list(_HEADING_LINE_RE.finditer(md))
        chunks: list[tuple[str, str]] = []
        if len(headings) >= 3:
            cur_title = sec.title or "Overview"
            cur_start = 0
            for m in headings:
                pos = m.start()
                if pos - cur_start >= TARGET_CHUNK_CHARS // 2:
                    piece = md[cur_start:pos].strip()
                    if piece:
                        chunks.append((cur_title, piece))
                    cur_title = m.group(2).strip()[:80]
                    cur_start = pos
            tail = md[cur_start:].strip()
            if tail:
                chunks.append((cur_title, tail))
        else:
            pos = 0
            part = 1
            while pos < len(md):
                end = min(pos + TARGET_CHUNK_CHARS, len(md))
                if end < len(md):
                    nl = md.rfind("\n\n", pos + TARGET_CHUNK_CHARS // 2, end)
                    if nl != -1:
                        end = nl
                piece = md[pos:end].strip()
                if piece:
                    chunks.append((f"{sec.title or 'Section'} (Part {part})", piece))
                    part += 1
                pos = end

        if not chunks:
            chunks = [(sec.title or sec.locator, md)]

        for idx, (chunk_title, chunk_md) in enumerate(chunks, start=1):
            expanded.append(
                VirtualSection(
                    locator=f"{sec.locator}#p{idx}",
                    title=chunk_title,
                    ordinal=ordinal,
                    markdown=chunk_md,
                    ocr_used=sec.ocr_used,
                    parent_locator=sec.locator,
                )
            )
            ordinal += 1
    return expanded


def count_query_hits(text: str, query: str) -> int:
    """Count case-insensitive occurrences of meaningful query tokens in `text`."""
    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{1,}", query)]
    if not terms or not text:
        return 0
    low = text.lower()
    return sum(low.count(term) for term in set(terms))


def compute_section_diff(
    old_markdown: str,
    new_markdown: str,
    *,
    changes_only: bool = False,
    max_chars: int = 80_000,
) -> dict[str, Any]:
    """Compute a paragraph-aligned word-level redline diff between two filing sections."""
    old_paras = [p.strip() for p in re.split(r"\n{2,}", old_markdown[:max_chars]) if p.strip()]
    new_paras = [p.strip() for p in re.split(r"\n{2,}", new_markdown[:max_chars]) if p.strip()]

    sm = difflib.SequenceMatcher(None, old_paras, new_paras, autojunk=False)
    hunks: list[dict[str, Any]] = []
    added_words = 0
    removed_words = 0

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            if not changes_only:
                for p in new_paras[j1:j2]:
                    hunks.append({"kind": "equal", "spans": [{"op": "equal", "text": p}]})
        elif tag == "insert":
            for p in new_paras[j1:j2]:
                added_words += len(p.split())
                hunks.append({"kind": "insert", "spans": [{"op": "insert", "text": p}]})
        elif tag == "delete":
            for p in old_paras[i1:i2]:
                removed_words += len(p.split())
                hunks.append({"kind": "delete", "spans": [{"op": "delete", "text": p}]})
        elif tag == "replace":
            old_block = "\n\n".join(old_paras[i1:i2])
            new_block = "\n\n".join(new_paras[j1:j2])
            old_tokens = _WORD_TOKEN_RE.findall(old_block)
            new_tokens = _WORD_TOKEN_RE.findall(new_block)
            wsm = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
            spans: list[dict[str, str]] = []
            for wtag, wi1, wi2, wj1, wj2 in wsm.get_opcodes():
                if wtag == "equal":
                    spans.append({"op": "equal", "text": "".join(new_tokens[wj1:wj2])})
                elif wtag == "delete":
                    txt = "".join(old_tokens[wi1:wi2])
                    removed_words += len(txt.split())
                    spans.append({"op": "delete", "text": txt})
                elif wtag == "insert":
                    txt = "".join(new_tokens[wj1:wj2])
                    added_words += len(txt.split())
                    spans.append({"op": "insert", "text": txt})
                elif wtag == "replace":
                    del_txt = "".join(old_tokens[wi1:wi2])
                    ins_txt = "".join(new_tokens[wj1:wj2])
                    removed_words += len(del_txt.split())
                    added_words += len(ins_txt.split())
                    if del_txt:
                        spans.append({"op": "delete", "text": del_txt})
                    if ins_txt:
                        spans.append({"op": "insert", "text": ins_txt})
            hunks.append({"kind": "replace", "spans": spans})

    return {
        "added_words": added_words,
        "removed_words": removed_words,
        "truncated": len(old_markdown) > max_chars or len(new_markdown) > max_chars,
        "hunks": hunks[:250],
    }


def _is_annual_flow_entry(entry: dict[str, Any], end: str) -> bool:
    """Return True if an income/cashflow entry represents a full-year (~330-380 day) period."""
    start = str(entry.get("start") or "").strip()
    if start:
        try:
            days = (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days
        except ValueError:
            return False
        return 330 <= days <= 380
    frame = str(entry.get("frame") or "").upper()
    if "Q" in frame:
        return False
    fp = str(entry.get("fp") or "").upper()
    return fp in {"", "FY"}


def build_xbrl_statements(
    facts_json: dict[str, Any],
    *,
    doc_id_by_accn: dict[str, str],
    max_periods: int = 6,
) -> dict[str, Any]:
    """Map raw SEC `companyfacts` JSON into standardized annual statement rows."""
    facts_root = facts_json.get("facts")
    if not isinstance(facts_root, dict):
        return {"periods": [], "rows": []}

    annual_forms = {"10-K", "20-F", "40-F", "10-K/A", "20-F/A", "40-F/A"}
    rows: list[dict[str, Any]] = []
    all_periods: set[str] = set()

    for key, label, statement, concepts in STANDARD_XBRL_METRICS:
        best_by_period: dict[str, dict[str, Any]] = {}
        is_flow = statement in ("income", "cashflow")
        preferred_unit: str | None = None

        for taxonomy in ("us-gaap", "ifrs-full"):
            tax_block = facts_root.get(taxonomy)
            if not isinstance(tax_block, dict):
                continue
            for concept_rank, concept in enumerate(concepts):
                cblock = tax_block.get(concept)
                if not isinstance(cblock, dict):
                    continue
                units = cblock.get("units")
                if not isinstance(units, dict):
                    continue
                for unit_name, entries in units.items():
                    if not isinstance(entries, list):
                        continue
                    if preferred_unit is not None and unit_name != preferred_unit:
                        continue
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        form = str(entry.get("form") or "")
                        if form not in annual_forms:
                            continue
                        end = str(entry.get("end") or "").strip()
                        filed = str(entry.get("filed") or "").strip()
                        val = entry.get("val")
                        if (
                            len(end) < 4
                            or not end[:4].isdigit()
                            or not isinstance(val, (int, float))
                        ):
                            continue
                        if is_flow and not _is_annual_flow_entry(entry, end):
                            continue
                        period_label = f"FY{end[:4]}"
                        if preferred_unit is None:
                            preferred_unit = unit_name
                        prev = best_by_period.get(period_label)
                        prev_filed = str(prev.get("filed") or "") if prev else ""
                        prev_end = str(prev.get("end") or "") if prev else ""
                        prev_rank = int(prev.get("_rank", 999)) if prev else 999
                        # Prefer latest filing date; break ties by latest end date, then concept priority.
                        if (
                            prev is None
                            or filed > prev_filed
                            or (filed == prev_filed and end > prev_end)
                            or (
                                filed == prev_filed and end == prev_end and concept_rank < prev_rank
                            )
                        ):
                            accn = str(entry.get("accn") or "")
                            best_by_period[period_label] = {
                                "val": val,
                                "unit": unit_name,
                                "form": form,
                                "end": end,
                                "filed": filed,
                                "accn": accn,
                                "document_id": doc_id_by_accn.get(accn),
                                "concept": f"{taxonomy}:{concept}",
                                "_rank": concept_rank,
                            }

        if best_by_period:
            all_periods.update(best_by_period.keys())
            latest_period = max(best_by_period.keys())
            latest_cell = best_by_period[latest_period]
            cleaned_values = {
                p: {k: v for k, v in cell.items() if k != "_rank"}
                for p, cell in best_by_period.items()
            }
            rows.append(
                {
                    "key": key,
                    "label": label,
                    "statement": statement,
                    "concept": latest_cell["concept"],
                    "unit": latest_cell["unit"],
                    "values": cleaned_values,
                }
            )

    sorted_periods = sorted(all_periods, reverse=True)[:max_periods]
    for row in rows:
        row["values"] = {p: row["values"][p] for p in sorted_periods if p in row["values"]}
    return {"periods": sorted_periods, "rows": rows}


def _find_previous_document_id(
    docs: Sequence[DocumentRecord], current: DocumentRecord
) -> str | None:
    """Find the immediately preceding filing of the same issuer and form family."""
    candidates = [
        d
        for d in docs
        if d.document_id != current.document_id
        and d.form_family == current.form_family
        and d.filing_date < current.filing_date
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda d: d.filing_date, reverse=True)
    return candidates[0].document_id


def _sanitize_download_filename(doc: DocumentRecord) -> str:
    mime = (doc.source_mime or "").lower()
    if "pdf" in mime:
        ext = ".pdf"
    elif "spreadsheetml" in mime or "excel" in mime:
        ext = ".xlsx"
    elif "html" in mime or "xhtml" in mime:
        ext = ".html"
    else:
        ext = ".bin"
    safe_base = re.sub(
        r"[^A-Za-z0-9._-]+", "_", f"{doc.source_key}_{doc.filing_date}_{doc.document_id}"
    )
    return f"{safe_base[:90]}{ext}"


WebAuthResolver = Callable[[Request], dict[str, Any] | None]


def _serialize_suite(suite: Suite | None) -> dict[str, Any] | None:
    """Serialize a visitor's suite navigation to plain JSON without endpoint rows."""
    if suite is None:
        return None
    return {
        "name": suite.name,
        "account": suite.account,
        "links": [
            {
                "label": link.label,
                "url": link.url,
                "section": link.section,
                "description": link.description,
                "current": link.current,
            }
            for link in suite.links
        ],
    }


def build_web_routes(
    service: FilingsService,
    *,
    browser_auth: BrowserAuth | None = None,
    web_auth_resolver: WebAuthResolver | None = None,
    public_base_url: str | None = None,
    static_dir: Path | None = None,
) -> list[BaseRoute]:
    """Build Starlette routes for the strictly read-only Web Workspace and JSON API."""
    resolved_static = static_dir or WEB_STATIC_DIR

    def _resolve_identity(request: Request) -> dict[str, Any] | None:
        if web_auth_resolver is not None:
            return web_auth_resolver(request)
        if browser_auth is not None:
            who = browser_auth.identity(request)
            return dict(who) if who else None
        return None

    def _require_subject_api(
        request: Request,
    ) -> tuple[str | None, dict[str, Any] | None, JSONResponse | None]:
        who = _resolve_identity(request)
        if not who or not who.get("sub"):
            return (
                None,
                None,
                JSONResponse(
                    {"error": "unauthorized", "detail": "Sign in required"},
                    status_code=401,
                    headers=API_SECURITY_HEADERS,
                ),
            )
        return str(who["sub"]), who, None

    def _json(payload: Any, status_code: int = 200) -> JSONResponse:
        return JSONResponse(payload, status_code=status_code, headers=API_SECURITY_HEADERS)

    async def _api_me(request: Request) -> Response:
        subject, who, err = _require_subject_api(request)
        if err is not None or subject is None or who is None:
            return err or _json({"error": "unauthorized"}, 401)
        try:
            st = await service.status(subject)
        except HubUnavailableError as exc:
            return _json({"error": "hub_unavailable", "detail": str(exc)}, 503)
        raw_claims = who.get("claims")
        claims: dict[str, Any] = raw_claims if isinstance(raw_claims, dict) else {}
        suite_payload = (
            _serialize_suite(browser_auth.suite(request)) if browser_auth is not None else None
        )
        return _json(
            {
                "subject": subject,
                "name": str(
                    who.get("label") or who.get("name") or claims.get("person_id") or "User"
                ),
                "person_id": str(claims.get("person_id") or ""),
                "company_count": st["company_count"],
                "covered_companies": sum(1 for c in st["companies"] if c["document_count"] > 0),
                "total_documents": sum(int(c["document_count"]) for c in st["companies"]),
                "public_base_url": public_base_url or "",
                "has_logout": browser_auth is not None,
                "suite": suite_payload,
            }
        )

    async def _api_companies(request: Request) -> Response:
        subject, _, err = _require_subject_api(request)
        if err is not None or subject is None:
            return err or _json({"error": "unauthorized"}, 401)
        try:
            st = await service.status(subject)
        except HubUnavailableError as exc:
            return _json({"error": "hub_unavailable", "detail": str(exc)}, 503)
        return _json(st)

    async def _api_documents(request: Request) -> Response:
        subject, _, err = _require_subject_api(request)
        if err is not None or subject is None:
            return err or _json({"error": "unauthorized"}, 401)
        source_key = request.query_params.get("source_key") or None
        form_family = request.query_params.get("form_family") or None
        since = request.query_params.get("since") or None
        try:
            limit = clamp_limit(int(request.query_params.get("limit", "100")))
        except ValueError:
            limit = 100
        try:
            docs = await service.list_documents(
                subject,
                source_key=source_key,
                form_family=form_family,
                since=since,
                limit=limit,
            )
        except HubUnavailableError as exc:
            return _json({"error": "hub_unavailable", "detail": str(exc)}, 503)
        enriched = []
        for d in docs:
            item = dict(d)
            item["display_form_type"] = format_display_form_type(
                str(d.get("form_type") or ""), str(d.get("adapter") or "")
            )
            item["viewer_url"] = build_viewer_url(public_base_url, str(d["document_id"]))
            enriched.append(item)
        return _json({"documents": enriched, "count": len(enriched)})

    async def _api_search(request: Request) -> Response:
        subject, _, err = _require_subject_api(request)
        if err is not None or subject is None:
            return err or _json({"error": "unauthorized"}, 401)
        query = (request.query_params.get("q") or "").strip()
        if not query:
            return _json({"query": "", "results": [], "count": 0})
        source_key = request.query_params.get("source_key") or None
        form_family = request.query_params.get("form_family") or None
        since = request.query_params.get("since") or None
        try:
            limit = clamp_limit(int(request.query_params.get("limit", "30")))
        except ValueError:
            limit = 30
        try:
            hits = await service.search(
                subject,
                query,
                source_key=source_key,
                form_family=form_family,
                since=since,
                limit=limit,
            )
        except HubUnavailableError as exc:
            return _json({"error": "hub_unavailable", "detail": str(exc)}, 503)
        enriched = []
        for h in hits:
            item = dict(h)
            item["display_form_type"] = format_display_form_type(str(h.get("form_type") or ""))
            item["viewer_url"] = build_viewer_url(
                public_base_url,
                str(h["document_id"]),
                locator=str(h["locator"]),
                query=query,
            )
            enriched.append(item)
        return _json({"query": query, "results": enriched, "count": len(enriched)})

    async def _api_document_detail(request: Request) -> Response:
        subject, _, err = _require_subject_api(request)
        if err is not None or subject is None:
            return err or _json({"error": "unauthorized"}, 401)
        document_id = request.path_params["document_id"]
        query = (request.query_params.get("q") or "").strip()
        try:
            allowed = await service._allowed_keys(subject)

            def _load() -> dict[str, Any] | None:
                doc = service.store.get_document(document_id)
                if doc is None or doc.source_key not in allowed:
                    return None
                raw_sections = service.store.get_sections(document_id)
                expanded = expand_sections_for_reader(raw_sections)
                company = service.store.get_company(doc.source_key)
                peer_docs = service.store.list_documents(
                    {doc.source_key},
                    source_key=doc.source_key,
                    form_family=doc.form_family,
                    limit=50,
                )
                prev_doc_id = _find_previous_document_id(peer_docs, doc)
                outline = [
                    {
                        "locator": s.locator,
                        "title": s.title,
                        "ordinal": s.ordinal,
                        "chars": len(s.markdown),
                        "ocr_used": s.ocr_used,
                        "hit_count": count_query_hits(s.markdown, query) if query else 0,
                    }
                    for s in expanded
                ]
                return {
                    "document": {
                        **doc.model_dump(),
                        "display_form_type": format_display_form_type(
                            doc.form_type, str(doc.adapter)
                        ),
                        "ticker": company.ticker if company else "",
                        "company_name": company.name if company else doc.source_key,
                        "has_raw_artifact": service.cas.has(doc.source_sha256),
                        "prev_document_id": prev_doc_id,
                        "peer_documents": [
                            {
                                "document_id": p.document_id,
                                "filing_date": p.filing_date,
                                "period_end": p.period_end,
                                "display_form_type": format_display_form_type(
                                    p.form_type, str(p.adapter)
                                ),
                            }
                            for p in peer_docs
                            if p.document_id != doc.document_id
                        ][:12],
                    },
                    "total_hits": sum(o["hit_count"] for o in outline),
                    "outline": outline,
                }

            result = await service._run_db(_load)
        except HubUnavailableError as exc:
            return _json({"error": "hub_unavailable", "detail": str(exc)}, 503)
        if result is None:
            return _json(
                {"error": "not_found", "detail": "Document not found or not accessible"}, 404
            )
        return _json(result)

    async def _api_document_section(request: Request) -> Response:
        subject, _, err = _require_subject_api(request)
        if err is not None or subject is None:
            return err or _json({"error": "unauthorized"}, 401)
        document_id = request.path_params["document_id"]
        locator = (request.query_params.get("locator") or "").strip()
        query = (request.query_params.get("q") or "").strip()
        try:
            allowed = await service._allowed_keys(subject)

            def _load() -> dict[str, Any] | None:
                doc = service.store.get_document(document_id)
                if doc is None or doc.source_key not in allowed:
                    return None
                expanded = expand_sections_for_reader(service.store.get_sections(document_id))
                if not expanded:
                    return {
                        "document_id": document_id,
                        "locator": "",
                        "title": doc.title,
                        "markdown": "",
                        "citation": f"[{doc.source_key} {doc.form_type} {doc.filing_date}]",
                        "viewer_url": build_viewer_url(public_base_url, document_id),
                    }
                chosen = next(
                    (s for s in expanded if s.locator == locator or s.parent_locator == locator),
                    expanded[0],
                )
                company = service.store.get_company(doc.source_key)
                label_ticker = company.ticker if company and company.ticker else doc.source_key
                disp_form = format_display_form_type(doc.form_type, str(doc.adapter))
                period = doc.period_end or doc.filing_date
                return {
                    "document_id": document_id,
                    "locator": chosen.locator,
                    "parent_locator": chosen.parent_locator,
                    "title": chosen.title,
                    "ordinal": chosen.ordinal,
                    "ocr_used": chosen.ocr_used,
                    "markdown": chosen.markdown,
                    "hit_count": count_query_hits(chosen.markdown, query) if query else 0,
                    "citation": f"[{label_ticker} {disp_form} {period} · {chosen.locator}]",
                    "viewer_url": build_viewer_url(
                        public_base_url, document_id, locator=chosen.locator, query=query or None
                    ),
                }

            result = await service._run_db(_load)
        except HubUnavailableError as exc:
            return _json({"error": "hub_unavailable", "detail": str(exc)}, 503)
        if result is None:
            return _json({"error": "not_found"}, 404)
        return _json(result)

    async def _api_document_diff(request: Request) -> Response:
        subject, _, err = _require_subject_api(request)
        if err is not None or subject is None:
            return err or _json({"error": "unauthorized"}, 401)
        new_id = request.path_params["document_id"]
        old_id = request.path_params["other_id"]
        locator = (request.query_params.get("locator") or "").strip()
        changes_only = request.query_params.get("changes_only", "1") not in ("0", "false", "False")
        try:
            allowed = await service._allowed_keys(subject)

            def _load() -> dict[str, Any] | None:
                new_doc = service.store.get_document(new_id)
                old_doc = service.store.get_document(old_id)
                if (
                    new_doc is None
                    or old_doc is None
                    or new_doc.source_key not in allowed
                    or old_doc.source_key not in allowed
                ):
                    return None
                new_secs = expand_sections_for_reader(service.store.get_sections(new_id))
                old_secs = expand_sections_for_reader(service.store.get_sections(old_id))
                new_match = next(
                    (s for s in new_secs if s.locator == locator or s.parent_locator == locator),
                    new_secs[0] if new_secs else None,
                )
                old_match = next(
                    (
                        s
                        for s in old_secs
                        if new_match is not None
                        and (
                            s.locator == new_match.locator
                            or s.parent_locator == new_match.parent_locator
                            or (s.title and s.title.lower() == new_match.title.lower())
                        )
                    ),
                    None,
                )
                diff_payload = compute_section_diff(
                    old_match.markdown if old_match else "",
                    new_match.markdown if new_match else "",
                    changes_only=changes_only,
                )
                return {
                    "new_document_id": new_id,
                    "old_document_id": old_id,
                    "new_filing_date": new_doc.filing_date,
                    "old_filing_date": old_doc.filing_date,
                    "locator": new_match.locator if new_match else locator,
                    "matched_old_locator": old_match.locator if old_match else None,
                    "changes_only": changes_only,
                    **diff_payload,
                }

            result = await service._run_db(_load)
        except HubUnavailableError as exc:
            return _json({"error": "hub_unavailable", "detail": str(exc)}, 503)
        if result is None:
            return _json({"error": "not_found"}, 404)
        return _json(result)

    async def _api_company_facts(request: Request) -> Response:
        subject, _, err = _require_subject_api(request)
        if err is not None or subject is None:
            return err or _json({"error": "unauthorized"}, 401)
        source_key = request.path_params["source_key"]
        try:
            allowed = await service._allowed_keys(subject)
            if source_key not in allowed:
                return _json({"error": "not_found"}, 404)

            def _load() -> dict[str, Any] | None:
                company = service.store.get_company(source_key)
                meta = service.store.get_company_facts(source_key)
                if meta is None:
                    return {
                        "source_key": source_key,
                        "ticker": company.ticker if company else "",
                        "name": company.name if company else source_key,
                        "has_facts": False,
                        "periods": [],
                        "rows": [],
                    }
                sha = str(meta["facts_sha256"])
                if not service.cas.has(sha):
                    return {
                        "source_key": source_key,
                        "ticker": company.ticker if company else "",
                        "name": company.name if company else source_key,
                        "has_facts": False,
                        "periods": [],
                        "rows": [],
                    }
                facts_json = json.loads(service.cas.get_bytes(sha).decode("utf-8"))
                docs = service.store.list_documents({source_key}, source_key=source_key, limit=200)
                doc_id_by_accn: dict[str, str] = {}
                for d in docs:
                    ext = d.external_id
                    doc_id_by_accn[ext] = d.document_id
                    if ":" in ext:
                        doc_id_by_accn[ext.split(":", 1)[0]] = d.document_id
                stmts = build_xbrl_statements(facts_json, doc_id_by_accn=doc_id_by_accn)
                return {
                    "source_key": source_key,
                    "ticker": company.ticker if company else "",
                    "name": company.name if company else source_key,
                    "cik": int(meta["cik"]),
                    "fetched_at": str(meta["fetched_at"]),
                    "has_facts": True,
                    **stmts,
                }

            result = await service._run_db(_load)
        except HubUnavailableError as exc:
            return _json({"error": "hub_unavailable", "detail": str(exc)}, 503)
        return _json(result)

    async def _api_document_raw(request: Request) -> Response:
        subject, _, err = _require_subject_api(request)
        if err is not None or subject is None:
            return err or _json({"error": "unauthorized"}, 401)
        document_id = request.path_params["document_id"]
        try:
            allowed = await service._allowed_keys(subject)

            def _load() -> tuple[DocumentRecord, bytes] | None:
                doc = service.store.get_document(document_id)
                if doc is None or doc.source_key not in allowed:
                    return None
                if not service.cas.has(doc.source_sha256):
                    return None
                return doc, service.cas.get_bytes(doc.source_sha256)

            loaded = await service._run_db(_load)
        except HubUnavailableError as exc:
            return _json({"error": "hub_unavailable", "detail": str(exc)}, 503)
        if loaded is None:
            return _json({"error": "not_found"}, 404)
        doc, payload = loaded
        mime = (doc.source_mime or "application/octet-stream").split(";")[0].strip().lower()
        filename = _sanitize_download_filename(doc)
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Disposition": f'inline; filename="{filename}"',
        }
        if mime in ("text/html", "application/xhtml+xml"):
            # Strictly sandbox untrusted issuer HTML: zero JS execution, zero cookies, zero network.
            headers["Content-Security-Policy"] = (
                "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; frame-ancestors 'self'"
            )
            media_type = "text/html; charset=utf-8"
        elif mime == "application/pdf":
            headers["Content-Security-Policy"] = (
                "default-src 'none'; object-src 'self'; frame-ancestors 'self'"
            )
            media_type = "application/pdf"
        else:
            headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
            media_type = "application/octet-stream"
        return Response(content=payload, media_type=media_type, headers=headers)

    async def _serve_spa(request: Request) -> Response:
        who = _resolve_identity(request)
        if not who or not who.get("sub"):
            if browser_auth is not None:
                next_url = request.url.path
                if request.url.query:
                    next_url = f"{next_url}?{request.url.query}"
                return RedirectResponse(
                    f"/connections/login?next={quote(next_url, safe='')}",
                    status_code=303,
                    headers={"Cache-Control": "no-store"},
                )
            return JSONResponse(
                {"error": "unauthorized", "detail": "Web UI authentication is not configured"},
                status_code=401,
                headers=API_SECURITY_HEADERS,
            )
        index_file = resolved_static / "index.html"
        if not index_file.is_file():
            return HTMLResponse(
                connection_page(
                    "Filings Library",
                    "<article><p>Web workspace assets are not built.</p></article>",
                    suite=browser_auth.suite(request) if browser_auth is not None else None,
                ),
                status_code=503,
                headers=PAGE_HEADERS,
            )
        return HTMLResponse(
            content=index_file.read_text(encoding="utf-8"),
            headers=PAGE_SECURITY_HEADERS,
        )

    async def _serve_asset(request: Request) -> Response:
        # Static assets require no secrets, but still prevent path traversal.
        rel = request.path_params["asset_path"]
        assets_root = (resolved_static / "assets").resolve()
        candidate = (assets_root / rel).resolve()
        if not candidate.is_relative_to(assets_root) or not candidate.is_file():
            return Response(status_code=404, headers=API_SECURITY_HEADERS)
        suffix = candidate.suffix.lower()
        media_types = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".json": "application/json; charset=utf-8",
            ".woff2": "font/woff2",
        }
        raw_bytes = candidate.read_bytes()
        etag = f'"{hashlib.sha256(raw_bytes).hexdigest()[:16]}"'
        asset_headers = {
            "Cache-Control": "no-cache",
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=asset_headers)
        return Response(
            content=raw_bytes,
            media_type=media_types.get(suffix, "application/octet-stream"),
            headers=asset_headers,
        )

    async def _connections_home(request: Request) -> Response:
        who = _resolve_identity(request)
        if not who or not who.get("sub"):
            if browser_auth is not None:
                return RedirectResponse(
                    "/connections/login",
                    status_code=303,
                    headers={"Cache-Control": "no-store"},
                )
            return JSONResponse(
                {"error": "unauthorized", "detail": "Web UI authentication is not configured"},
                status_code=401,
                headers=API_SECURITY_HEADERS,
            )
        return RedirectResponse(
            "/",
            status_code=308,
            headers={"Cache-Control": "no-store"},
        )

    routes: list[BaseRoute] = [
        Route("/api/me", _api_me, methods=["GET"]),
        Route("/api/companies", _api_companies, methods=["GET"]),
        Route("/api/companies/{source_key}/facts", _api_company_facts, methods=["GET"]),
        Route("/api/documents", _api_documents, methods=["GET"]),
        Route("/api/documents/{document_id}", _api_document_detail, methods=["GET"]),
        Route("/api/documents/{document_id}/section", _api_document_section, methods=["GET"]),
        Route("/api/documents/{document_id}/diff/{other_id}", _api_document_diff, methods=["GET"]),
        Route("/api/documents/{document_id}/raw", _api_document_raw, methods=["GET"]),
        Route("/api/search", _api_search, methods=["GET"]),
        Route("/assets/{asset_path:path}", _serve_asset, methods=["GET"]),
        Route("/d/{document_id}", _serve_spa, methods=["GET"]),
        Route("/connections", _connections_home, methods=["GET"]),
        Route("/connections/", _connections_home, methods=["GET"]),
        Route("/", _serve_spa, methods=["GET"]),
    ]
    if browser_auth is not None:
        routes.extend(browser_auth.routes("/connections"))
    return routes
