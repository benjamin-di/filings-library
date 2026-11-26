"""HKEXnews adapter with strict `t1code`/`t2code` and title noise filtering."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from filings_library.adapters import FilingCandidate
from filings_library.models import CompanyTarget, FormFamily, SourceAdapter
from filings_library.url_safety import (
    HKEX_ALLOWED_HOSTS,
    HostResolver,
    UnsafeUrlError,
    validate_public_https_url,
)

HKEX_STOCK_LIST_URL = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json"
HKEX_SEARCH_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
HKEX_DOC_BASE = "https://www1.hkexnews.hk"

# Strictly rejected HKEX categories (noise).
_REJECTED_T1_CODES = frozenset({"19000"})  # Overseas Regulatory Announcements
_REJECTED_T2_CODES = frozenset(
    {
        "13100",  # Date of Board Meeting
        "16300",  # Next Day Disclosure Returns
        "16400",  # Monthly Returns
        "19100",  # Overseas Regulatory Announcement
    }
)
_REJECTED_TITLE_SUBSTRINGS = (
    "monthly return",
    "next day disclosure return",
    "date of board meeting",
    "board meeting",
    "overseas regulatory announcement",
    "poll results of",
    "form of proxy",
    "notification letter",
)

# Kept HKEX categories mapped to FormFamily.
_FINANCIAL_STATEMENTS_T1 = "40000"
_FINANCIAL_STATEMENTS_T2: dict[str, FormFamily] = {
    "40100": FormFamily.ANNUAL_REPORT,
    "40200": FormFamily.INTERIM_REPORT,
    "40300": FormFamily.QUARTERLY_REPORT,
    "40400": FormFamily.ANNUAL_REPORT,  # ESG report
}
_RESULTS_T2: dict[str, FormFamily] = {
    "13300": FormFamily.RESULTS_RELEASE,  # Final / Annual Results
    "13400": FormFamily.RESULTS_RELEASE,  # Interim Results
    "13500": FormFamily.RESULTS_RELEASE,  # Quarterly Results
    "13600": FormFamily.RESULTS_RELEASE,  # Results Announcement
}
_CIRCULAR_T1 = "50000"
_OFFERING_T1 = frozenset({"20000", "51000"})
_OFFERING_T2 = frozenset({"20100", "51500"})

_HKEX_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def is_kept_hkex_filing(t1code: str, t2code: str, title: str) -> tuple[bool, FormFamily]:
    """Return `(kept, form_family)` enforcing strict HKEX noise rejection."""
    t1 = str(t1code or "").strip()
    t2 = str(t2code or "").strip()
    lower_title = (title or "").strip().lower()

    if t1 in _REJECTED_T1_CODES or t2 in _REJECTED_T2_CODES:
        return False, FormFamily.OTHER
    if any(noise in lower_title for noise in _REJECTED_TITLE_SUBSTRINGS):
        return False, FormFamily.OTHER

    if t2 in _FINANCIAL_STATEMENTS_T2:
        return True, _FINANCIAL_STATEMENTS_T2[t2]
    if t1 == _FINANCIAL_STATEMENTS_T1:
        if "interim" in lower_title:
            return True, FormFamily.INTERIM_REPORT
        if "quarter" in lower_title:
            return True, FormFamily.QUARTERLY_REPORT
        return True, FormFamily.ANNUAL_REPORT

    if t2 in _RESULTS_T2:
        return True, _RESULTS_T2[t2]

    if t1 == _CIRCULAR_T1 or t2.startswith("50"):
        return True, FormFamily.CIRCULAR

    if t1 in _OFFERING_T1 or t2 in _OFFERING_T2:
        return True, FormFamily.OFFERING

    return False, FormFamily.OTHER


def _parse_hkex_date(raw: str) -> str:
    match = _HKEX_DATE_RE.search(raw or "")
    if match:
        day, month, year = match.group(1), match.group(2), match.group(3)
        return f"{year}-{month}-{day}"
    try:
        return datetime.fromisoformat(raw[:10]).date().isoformat()
    except ValueError:
        return ""


def _guess_mime(path: str) -> str:
    lower = path.lower()
    if lower.endswith((".htm", ".html")):
        return "text/html"
    if lower.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return "application/pdf"


class HkexnewsAdapter:
    """HKEXnews structured disclosure adapter with strict noise filtering."""

    def __init__(
        self,
        *,
        backfill_years: int = 3,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: HostResolver | None = None,
    ) -> None:
        self._backfill_years = max(1, backfill_years)
        self._timeout = timeout
        self._transport = transport
        self._resolver = resolver

    async def resolve_stock_id(self, client: httpx.AsyncClient, ticker: str) -> str | None:
        """Resolve an HKEX numeric stock code (e.g. `0700` or `00700`) to `stockId`."""
        normalized = ticker.split(".", 1)[0].strip().lstrip("0")
        if not normalized.isdigit():
            return None
        code5 = normalized.zfill(5)
        resp = await client.get(HKEX_STOCK_LIST_URL)
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list):
            return None
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("c") or "").strip().zfill(5)
            if code == code5:
                return str(entry.get("i") or "").strip() or None
        return None

    async def discover(
        self, company: CompanyTarget, *, today: date | None = None
    ) -> list[FilingCandidate]:
        is_hk = (
            bool(company.hkex_stock_id)
            or company.exchange.upper() in {"XHKG", "HKEX", "SEHK"}
            or bool(re.fullmatch(r"\d{5}", company.ticker.strip()))
        )
        if not is_hk:
            return []

        ref_date = today or date.today()
        from_date = ref_date - timedelta(days=365 * self._backfill_years)

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            stock_id = company.hkex_stock_id
            if not stock_id:
                stock_id = await self.resolve_stock_id(client, company.ticker)
            if not stock_id:
                return []

            params = {
                "sortDir": "0",
                "sortByOptions": "DateTime",
                "category": "0",
                "market": "SEHK",
                "stockId": str(stock_id),
                "documentType": "-1",
                "fromDate": from_date.strftime("%Y%m%d"),
                "toDate": ref_date.strftime("%Y%m%d"),
                "title": "",
                "searchType": "0",
                "t1code": "-2",
                "t2Gcode": "-2",
                "t2code": "-2",
                "rowRange": "200",
                "lang": "E",
            }
            resp = await client.get(HKEX_SEARCH_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()

        raw_result = payload.get("result", [])
        if isinstance(raw_result, str):
            records: list[dict[str, Any]] = json.loads(raw_result) if raw_result.strip() else []
        elif isinstance(raw_result, list):
            records = raw_result
        else:
            records = []

        cutoff = from_date.isoformat()
        candidates: list[FilingCandidate] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            t1 = str(rec.get("t1_code") or rec.get("T1_CODE") or rec.get("t1code") or "")
            t2 = str(rec.get("t2_code") or rec.get("T2_CODE") or rec.get("t2code") or "")
            title = str(rec.get("TITLE") or rec.get("title") or "").strip()
            kept, form_family = is_kept_hkex_filing(t1, t2, title)
            if not kept:
                continue

            file_link = str(rec.get("FILE_LINK") or rec.get("file_link") or "").strip()
            if not file_link:
                continue
            raw_url = (
                file_link
                if file_link.startswith(("http://", "https://"))
                else f"{HKEX_DOC_BASE}{file_link if file_link.startswith('/') else '/' + file_link}"
            )
            try:
                url = validate_public_https_url(
                    raw_url,
                    allowed_hosts=HKEX_ALLOWED_HOSTS,
                    resolver=self._resolver,
                )
            except UnsafeUrlError:
                continue
            fdate = _parse_hkex_date(str(rec.get("DATE_TIME") or rec.get("date_time") or ""))
            if not fdate or fdate < cutoff:
                continue

            news_id = str(rec.get("NEWS_ID") or rec.get("news_id") or file_link).strip()
            form_type = str(rec.get("SHORT_TEXT") or form_family.value).strip()

            candidates.append(
                FilingCandidate(
                    source_key=company.source_key,
                    adapter=SourceAdapter.HKEXNEWS,
                    external_id=news_id,
                    form_type=form_type,
                    form_family=form_family,
                    title=title or form_type,
                    filing_date=fdate,
                    period_end=None,
                    source_url=url,
                    source_mime=_guess_mime(file_link),
                    language="en",
                    refetchable=True,
                    is_amendment=False,
                )
            )

        return candidates
