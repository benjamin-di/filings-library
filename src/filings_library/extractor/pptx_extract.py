"""MIT-licensed PowerPoint (.pptx) presentation extractor producing `slide:N` locators."""

from __future__ import annotations

import io
from typing import Any

from filings_library.models import ExtractedDocument, ExtractedSection

CONVERTER_ID = "pptx-extract"
CONVERTER_VERSION = "1.0.0"


def _table_to_markdown(table: Any) -> str:
    rows: list[list[str]] = []
    for row in table.rows:
        cells = [" ".join(cell.text.split()).replace("|", "\\|") for cell in row.cells]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    lines = [
        "| " + " | ".join(padded[0]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for r in padded[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def extract_pptx(data: bytes) -> ExtractedDocument:
    """Extract slide titles, text frames, speaker notes, and tables from a `.pptx` deck."""
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    sections: list[ExtractedSection] = []
    md_parts: list[str] = []

    for idx, slide in enumerate(prs.slides, start=1):
        title = ""
        title_shape = getattr(slide.shapes, "title", None)
        if title_shape is not None and getattr(title_shape, "has_text_frame", False):
            title = " ".join(title_shape.text_frame.text.split())[:160]

        blocks: list[str] = []
        if title:
            blocks.append(f"## Slide {idx}: {title}")
        else:
            blocks.append(f"## Slide {idx}")

        for shape in slide.shapes:
            if shape is title_shape:
                continue
            if getattr(shape, "has_text_frame", False):
                lines = [
                    " ".join(p.text.split())
                    for p in shape.text_frame.paragraphs
                    if p.text and p.text.strip()
                ]
                if lines:
                    blocks.append("\n".join(lines))
            if getattr(shape, "has_table", False):
                table_md = _table_to_markdown(shape.table)
                if table_md:
                    blocks.append(table_md)

        if getattr(slide, "has_notes_slide", False):
            notes_tf = getattr(slide.notes_slide, "notes_text_frame", None)
            if notes_tf is not None and notes_tf.text and notes_tf.text.strip():
                notes_clean = " ".join(notes_tf.text.split())
                blocks.append(f"_Speaker notes:_ {notes_clean}")

        slide_md = "\n\n".join(blocks).strip()
        sections.append(
            ExtractedSection(
                locator=f"slide:{idx}",
                title=title or f"Slide {idx}",
                ordinal=idx - 1,
                markdown=slide_md,
                ocr_used=False,
            )
        )
        md_parts.append(slide_md)

    return ExtractedDocument(
        converter_id=CONVERTER_ID,
        converter_version=CONVERTER_VERSION,
        locator_kind="page",
        markdown="\n\n---\n\n".join(md_parts) + ("\n" if md_parts else ""),
        sections=sections,
        page_count=len(sections),
        ocr_pages=[],
        warnings=[],
    )
