"""Openpyxl XLSX workbook to Markdown sheet index + sheet tables."""

from __future__ import annotations

import io
import re

import openpyxl

from filings_library.extractor.html_ixbrl import UNTRUSTED_HEADER
from filings_library.models import ExtractedDocument, ExtractedSection

CONVERTER_ID = "xlsx-openpyxl"
CONVERTER_VERSION = "1.0.0"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-")[:56] or "sheet"


def _fmt_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").replace("|", "\\|").strip()


def extract_xlsx(
    data: bytes,
    *,
    max_rows_per_sheet: int = 500,
    max_cols_per_sheet: int = 50,
) -> ExtractedDocument:
    """Extract an XLSX workbook into a sheet index plus per-sheet Markdown tables."""
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    warnings: list[str] = []
    sheet_sections: list[ExtractedSection] = []
    index_lines = [
        "## Sheet Index",
        "",
        "| Sheet | Rows | Columns | Locator |",
        "| --- | --- | --- | --- |",
    ]
    seen_locators: dict[str, int] = {}

    try:
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows: list[list[str]] = []
            truncated_rows = False
            truncated_cols = False
            max_cols = 0

            for r_idx, raw_row in enumerate(ws.iter_rows(values_only=True)):
                if r_idx >= max_rows_per_sheet:
                    truncated_rows = True
                    break
                if len(raw_row) > max_cols_per_sheet:
                    truncated_cols = True
                    raw_row = raw_row[:max_cols_per_sheet]
                formatted = [_fmt_cell(v) for v in raw_row]
                # Trim trailing empty cells
                while formatted and formatted[-1] == "":
                    formatted.pop()
                if formatted:
                    max_cols = max(max_cols, len(formatted))
                    rows.append(formatted)

            base_loc = f"section:sheet-{_slugify(sheet_name)}"
            cnt = seen_locators.get(base_loc, 0) + 1
            seen_locators[base_loc] = cnt
            locator = base_loc if cnt == 1 else f"{base_loc}-{cnt}"

            if truncated_rows:
                warnings.append(f"Sheet {sheet_name!r} truncated at {max_rows_per_sheet} rows")
            if truncated_cols:
                warnings.append(f"Sheet {sheet_name!r} truncated at {max_cols_per_sheet} columns")

            index_lines.append(
                f"| {_fmt_cell(sheet_name)} | {len(rows)} | {max_cols} | `{locator}` |"
            )

            if not rows or max_cols == 0:
                sheet_md = f"## {sheet_name}\n\n_(empty sheet)_"
            else:
                padded = [r + [""] * (max_cols - len(r)) for r in rows]
                header = padded[0]
                sep = ["---"] * max_cols
                lines = [
                    f"## {sheet_name}",
                    "",
                    "| " + " | ".join(header) + " |",
                    "| " + " | ".join(sep) + " |",
                ]
                for row in padded[1:]:
                    lines.append("| " + " | ".join(row) + " |")
                sheet_md = "\n".join(lines)

            sheet_sections.append(
                ExtractedSection(
                    locator=locator,
                    title=sheet_name,
                    ordinal=len(sheet_sections) + 2,
                    markdown=sheet_md,
                    ocr_used=False,
                )
            )
    finally:
        wb.close()

    index_section = ExtractedSection(
        locator="section:sheet-index",
        title="Sheet Index",
        ordinal=1,
        markdown="\n".join(index_lines),
        ocr_used=False,
    )
    sections = [index_section, *sheet_sections]

    md_parts = [UNTRUSTED_HEADER]
    for sec in sections:
        md_parts.append(f"<!-- {sec.locator} -->\n{sec.markdown}")

    return ExtractedDocument(
        converter_id=CONVERTER_ID,
        converter_version=CONVERTER_VERSION,
        locator_kind="section",
        markdown="\n\n".join(md_parts).strip() + "\n",
        sections=sections,
        page_count=None,
        ocr_pages=[],
        warnings=warnings,
    )
