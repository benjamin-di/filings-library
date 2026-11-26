"""Scoped FTS5 financial search and bounded document reader."""

from __future__ import annotations

import re
from collections.abc import Collection
from datetime import date
from typing import Any

from filings_library.models import Citation, FormFamily
from filings_library.store import FilingsStore

_FORM_FAMILY_WEIGHTS: dict[str, float] = {
    FormFamily.ANNUAL_REPORT.value: 1.40,
    FormFamily.INTERIM_REPORT.value: 1.20,
    FormFamily.QUARTERLY_REPORT.value: 1.20,
    FormFamily.RESULTS_RELEASE.value: 1.05,
    FormFamily.CURRENT_REPORT.value: 1.00,
    FormFamily.PROXY.value: 0.85,
    FormFamily.OFFERING.value: 0.90,
    FormFamily.CIRCULAR.value: 0.90,
    FormFamily.PRESENTATION.value: 0.95,
    FormFamily.DATABOOK.value: 0.95,
    FormFamily.OTHER.value: 0.80,
}

_FORMATTED_NUM_TOKEN_RE = re.compile(r"^[($€£¥]*(\d{1,3}(?:,\d{3})+(?:\.\d+)?)[)%]*$")
_QUOTED_OR_WORD_RE = re.compile(r'"([^"]+)"|(\S+)')
_WORD_RE = re.compile(r"[a-z0-9]+")


class DocumentNotFoundError(KeyError):
    """Raised when a document does not exist or is outside the caller's allowed scope."""


def normalize_query(query: str) -> str:
    """Convert a user query into a safe FTS5 MATCH query with numeric normalization."""
    terms: list[str] = []
    for match in _QUOTED_OR_WORD_RE.finditer(query.strip()):
        quoted, raw = match.group(1), match.group(2)
        if quoted is not None:
            cleaned = quoted.replace('"', " ").strip()
            if cleaned:
                terms.append(f'"{cleaned}"')
            continue
        if raw is None:
            continue
        num_match = _FORMATTED_NUM_TOKEN_RE.match(raw)
        if num_match:
            normalized = num_match.group(1).replace(",", "")
            terms.append(f'"{normalized}"')
            continue
        # Strip leading/trailing currency/punctuation while keeping internal alphanumeric tokens
        cleaned = re.sub(r"[^\w.\-]", "", raw)
        if not cleaned:
            continue
        if cleaned.upper() in {"AND", "OR", "NOT", "NEAR"}:
            terms.append(f'"{cleaned}"')
        elif "." in cleaned or "-" in cleaned:
            terms.append(f'"{cleaned}"')
        else:
            terms.append(cleaned)
    return " ".join(terms)


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _recency_multiplier(filing_date: str | None, reference_date: date) -> float:
    parsed = _parse_iso_date(filing_date)
    if parsed is None:
        return 0.70
    age_days = max(0, (reference_date - parsed).days)
    # 2-year half-life, floored at 0.35 so older annual reports remain discoverable.
    decay = 0.5 ** (age_days / 730.0)
    return float(max(0.35, min(1.0, decay)))


def _boilerplate_shingles(text: str) -> set[str]:
    words = _WORD_RE.findall(text.lower())
    if len(words) < 4:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + 4]) for i in range(len(words) - 3)}


def _is_near_duplicate(shingles_a: set[str], shingles_b: set[str], threshold: float = 0.80) -> bool:
    if not shingles_a or not shingles_b:
        return False
    inter = len(shingles_a & shingles_b)
    union = len(shingles_a | shingles_b)
    return (inter / union) >= threshold if union else False


def _make_snippet(text: str, query_tokens: list[str], max_len: int = 240) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_len:
        return compact
    lower = compact.lower()
    pos = -1
    for tok in query_tokens:
        idx = lower.find(tok)
        if idx != -1:
            pos = idx
            break
    if pos == -1:
        return compact[: max_len - 1].rstrip() + "…"
    start = max(0, pos - 60)
    end = min(len(compact), start + max_len)
    snippet = compact[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(compact):
        snippet = snippet + "…"
    return snippet


def search_filings(
    store: FilingsStore,
    query: str,
    *,
    allowed_source_keys: Collection[str],
    source_key: str | None = None,
    form_family: FormFamily | str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[Citation]:
    """Search filings strictly within `allowed_source_keys` using financial ranking."""
    allowed = sorted(set(allowed_source_keys))
    if not allowed or limit <= 0:
        return []
    if source_key is not None:
        if source_key not in allowed:
            return []
        allowed = [source_key]

    fts_query = normalize_query(query)
    if not fts_query:
        return []

    placeholders = ",".join("?" for _ in allowed)
    clauses = [
        "fts_chunks MATCH ?",
        f"d.source_key IN ({placeholders})",
    ]
    params: list[Any] = [fts_query, *allowed]
    if form_family is not None:
        clauses.append("d.form_family = ?")
        params.append(str(form_family))
    if since is not None:
        clauses.append("d.filing_date >= ?")
        params.append(since)

    candidate_limit = max(limit * 6, 120)
    params.append(candidate_limit)

    sql = f"""
        SELECT
            d.document_id,
            d.source_key,
            d.form_type,
            d.form_family,
            d.period_end,
            d.filing_date,
            d.source_sha256,
            d.source_url,
            COALESCE(d.converter_id, 'unknown') AS converter_id,
            d.is_amendment,
            fts_chunks.locator,
            fts_chunks.title,
            fts_chunks.body,
            bm25(fts_chunks, 2.0, 1.0, 1.5) AS raw_bm25
        FROM fts_chunks
        JOIN documents d ON d.document_id = fts_chunks.document_id
        WHERE {" AND ".join(clauses)}
        ORDER BY raw_bm25 ASC
        LIMIT ?
    """
    rows = store.conn.execute(sql, params).fetchall()
    if not rows:
        return []

    # Identify superseded original document IDs within the allowed scope.
    superseded_rows = store.conn.execute(
        f"""
        SELECT DISTINCT supersedes_document_id
        FROM documents
        WHERE source_key IN ({placeholders})
          AND supersedes_document_id IS NOT NULL
        """,
        allowed,
    ).fetchall()
    superseded_ids = {str(r["supersedes_document_id"]) for r in superseded_rows}

    parsed_dates = [_parse_iso_date(r["filing_date"]) for r in rows]
    valid_dates = [d for d in parsed_dates if d is not None]
    reference_date = max(valid_dates) if valid_dates else date.today()
    if date.today() > reference_date:
        reference_date = date.today()

    query_tokens = [t.strip('"').lower() for t in fts_query.split() if t.strip('"')]

    scored: list[tuple[float, str, str, str, set[str], Citation]] = []
    for r in rows:
        raw_bm25 = float(r["raw_bm25"])
        # SQLite bm25() is negative (more negative = better match).
        bm25_rel = max(0.1, -raw_bm25 + 1.0)
        recency = _recency_multiplier(r["filing_date"], reference_date)
        family_weight = _FORM_FAMILY_WEIGHTS.get(str(r["form_family"]), 0.85)
        doc_id = str(r["document_id"])
        if doc_id in superseded_ids:
            supersession_weight = 0.30
        elif bool(r["is_amendment"]):
            supersession_weight = 1.08
        else:
            supersession_weight = 1.0

        score = round(bm25_rel * recency * family_weight * supersession_weight, 6)
        body_text = str(r["body"])
        snippet = _make_snippet(body_text, query_tokens)
        citation = Citation(
            document_id=doc_id,
            source_key=str(r["source_key"]),
            form_type=str(r["form_type"]),
            period_end=r["period_end"],
            filing_date=str(r["filing_date"]),
            source_sha256=str(r["source_sha256"]),
            locator=str(r["locator"]),
            source_url=str(r["source_url"]),
            converter_id=str(r["converter_id"]),
            snippet=snippet,
            score=score,
        )
        scored.append(
            (
                score,
                str(r["filing_date"]),
                doc_id,
                str(r["locator"]),
                _boilerplate_shingles(body_text),
                citation,
            )
        )

    scored.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    # Sort descending by score, then descending by filing_date, then doc_id/locator asc
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    results: list[Citation] = []
    seen_shingles_by_company: dict[str, list[set[str]]] = {}
    for _score, _fdate, _doc_id, _loc, shingles, citation in scored:
        prior_list = seen_shingles_by_company.setdefault(citation.source_key, [])
        if any(_is_near_duplicate(shingles, prev) for prev in prior_list):
            continue
        prior_list.append(shingles)
        results.append(citation)
        if len(results) >= limit:
            break

    return results


def read_document(
    store: FilingsStore,
    *,
    document_id: str,
    allowed_source_keys: Collection[str],
    start_locator: str | None = None,
    end_locator: str | None = None,
    max_chars: int = 24000,
) -> dict[str, Any]:
    """Read bounded sections of a document with strict `allowed_source_keys` enforcement."""
    allowed = set(allowed_source_keys)
    doc = store.get_document(document_id)
    if doc is None or doc.source_key not in allowed:
        raise DocumentNotFoundError(f"Document not found: {document_id}")

    all_sections = store.get_sections(document_id)
    selected = all_sections

    if start_locator is not None:
        start_idx = next(
            (i for i, s in enumerate(selected) if s.locator == start_locator),
            None,
        )
        selected = selected[start_idx:] if start_idx is not None else []

    if end_locator is not None and selected:
        end_idx = next(
            (i for i, s in enumerate(selected) if s.locator == end_locator),
            None,
        )
        if end_idx is not None:
            selected = selected[: end_idx + 1]

    out_sections: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    total_chars = 0
    truncated = False
    converter_id = doc.converter_id or "unknown"

    for sec in selected:
        remaining = max_chars - total_chars
        if remaining <= 0:
            truncated = True
            break
        text = sec.markdown
        if len(text) > remaining:
            text = text[:remaining]
            truncated = True
        total_chars += len(text)
        out_sections.append(
            {
                "locator": sec.locator,
                "title": sec.title,
                "ordinal": sec.ordinal,
                "markdown": text,
                "ocr_used": sec.ocr_used,
            }
        )
        citation = Citation(
            document_id=doc.document_id,
            source_key=doc.source_key,
            form_type=doc.form_type,
            period_end=doc.period_end,
            filing_date=doc.filing_date,
            source_sha256=doc.source_sha256,
            locator=sec.locator,
            source_url=doc.source_url,
            converter_id=converter_id,
            snippet=_make_snippet(text, [], max_len=200),
            score=1.0,
        )
        citations.append(citation.model_dump())
        if truncated:
            break

    return {
        "document": doc.model_dump(),
        "sections": out_sections,
        "citations": citations,
        "truncated": truncated,
        "total_chars": total_chars,
    }
