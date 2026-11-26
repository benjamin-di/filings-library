"""MIT/Apache PDF extraction pipeline (qpdf + pdftotext + pypdfium2 + tesseract)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from filings_library.extractor.html_ixbrl import UNTRUSTED_HEADER
from filings_library.models import ExtractedDocument, ExtractedSection

CONVERTER_ID = "pdf-poppler-tesseract"
CONVERTER_VERSION = "1.0.0"

QPDF_TIMEOUT_SECONDS = 60
PDFTOTEXT_TIMEOUT_SECONDS = 120
TESSERACT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_OCR_PAGES = 50

_PASSWORD_MARKERS = (
    "invalid password",
    "incorrect password",
    "password required",
    "requires a password",
    "encrypted",
)


class EncryptedPdfError(ValueError):
    """Raised when a PDF is user-password-protected and cannot be unlocked with empty password."""


def _is_password_error(message: str) -> bool:
    lower = message.lower()
    return any(marker in lower for marker in _PASSWORD_MARKERS)


def _unlock_with_qpdf(
    src: Path, dst: Path, warnings: list[str], *, timeout: float = QPDF_TIMEOUT_SECONDS
) -> Path:
    qpdf = shutil.which("qpdf")
    if not qpdf:
        warnings.append("qpdf not installed; skipping empty-password unlock")
        return src
    try:
        proc = subprocess.run(
            [qpdf, "--password=", "--decrypt", str(src), str(dst)],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        warnings.append(f"qpdf timed out after {timeout}s; using original PDF")
        return src
    if proc.returncode == 0 and dst.exists():
        return dst
    stderr = (proc.stderr or "") + (proc.stdout or "")
    # qpdf exit code 3 is warnings-only (output still written).
    if proc.returncode == 3 and dst.exists():
        warnings.append(f"qpdf warning: {stderr.strip()}")
        return dst
    if _is_password_error(stderr):
        raise EncryptedPdfError(f"PDF requires a user password: {stderr.strip()}")
    warnings.append(f"qpdf unlock failed (exit {proc.returncode}); using original PDF")
    return src


def _extract_native_pages_pdftotext(
    pdf_path: Path,
    warnings: list[str] | None = None,
    *,
    timeout: float = PDFTOTEXT_TIMEOUT_SECONDS,
) -> list[str] | None:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return None
    try:
        proc = subprocess.run(
            [pdftotext, "-layout", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if warnings is not None:
            warnings.append(f"pdftotext timed out after {timeout}s")
        return None
    if proc.returncode != 0:
        stderr = (proc.stderr or "") + (proc.stdout or "")
        if _is_password_error(stderr):
            raise EncryptedPdfError(f"PDF requires a user password: {stderr.strip()}")
        return None
    raw = proc.stdout
    # pdftotext separates pages with form-feed (\f), with a trailing \f after the last page.
    pages = raw.split("\f")
    if pages and pages[-1] == "":
        pages.pop()
    return pages


def _extract_native_pages_pypdfium2(pdf_path: Path) -> list[str] | None:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:
        if _is_password_error(str(exc)):
            raise EncryptedPdfError(f"PDF requires a user password: {exc}") from exc
        raise
    try:
        pages: list[str] = []
        for idx in range(len(doc)):
            page = doc[idx]
            textpage = page.get_textpage()
            try:
                pages.append(textpage.get_text_range())
            finally:
                textpage.close()
                page.close()
        return pages
    finally:
        doc.close()


def _ocr_page(
    pdf_path: Path,
    page_index: int,
    tmp_dir: Path,
    warnings: list[str],
    *,
    timeout: float = TESSERACT_TIMEOUT_SECONDS,
) -> str | None:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        msg = "tesseract not installed; skipping OCR for low-text pages"
        if msg not in warnings:
            warnings.append(msg)
        return None
    try:
        import pypdfium2 as pdfium
    except ImportError:
        msg = "pypdfium2 not installed; cannot render pages for OCR"
        if msg not in warnings:
            warnings.append(msg)
        return None

    img_path = tmp_dir / f"page_{page_index + 1}.png"
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            page = doc[page_index]
            try:
                bitmap = page.render(scale=200 / 72.0)
                pil_image = bitmap.to_pil()
                pil_image.save(img_path)
            finally:
                page.close()
        finally:
            doc.close()

        try:
            proc = subprocess.run(
                [tesseract, str(img_path), "stdout", "--psm", "6"],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            warnings.append(f"tesseract timed out on page {page_index + 1} after {timeout}s")
            return None
        if proc.returncode != 0:
            warnings.append(
                f"tesseract failed on page {page_index + 1}: {(proc.stderr or '').strip()}"
            )
            return None
        return proc.stdout.strip()
    finally:
        img_path.unlink(missing_ok=True)


def extract_pdf(
    data: bytes,
    *,
    min_native_chars: int = 30,
    max_ocr_pages: int = DEFAULT_MAX_OCR_PAGES,
) -> ExtractedDocument:
    """Extract PDF bytes into per-page Markdown sections with `page:N` locators."""
    if not data.startswith(b"%PDF"):
        raise ValueError("Input bytes do not appear to be a valid PDF")

    warnings: list[str] = []
    ocr_pages: list[int] = []
    ocr_attempts = 0
    ocr_cap_warned = False

    with tempfile.TemporaryDirectory(prefix="filings_pdf_") as tmp:
        tmp_dir = Path(tmp)
        raw_path = tmp_dir / "input.pdf"
        unlocked_path = tmp_dir / "unlocked.pdf"
        raw_path.write_bytes(data)

        work_path = _unlock_with_qpdf(raw_path, unlocked_path, warnings)

        pages = _extract_native_pages_pdftotext(work_path, warnings)
        if pages is None:
            warnings.append("pdftotext unavailable or failed; falling back to pypdfium2 text layer")
            pages = _extract_native_pages_pypdfium2(work_path)
        if pages is None:
            raise RuntimeError(
                "No PDF text extractor available (install poppler-utils or pypdfium2)"
            )

        sections: list[ExtractedSection] = []
        md_blocks: list[str] = [UNTRUSTED_HEADER]

        for idx, native_text in enumerate(pages):
            page_num = idx + 1
            cleaned = native_text.strip()
            ocr_used = False
            non_ws_len = len("".join(cleaned.split()))
            if non_ws_len < min_native_chars:
                if ocr_attempts >= max_ocr_pages:
                    if not ocr_cap_warned:
                        warnings.append(
                            f"OCR page cap ({max_ocr_pages}) exceeded; "
                            "skipping OCR for remaining low-text pages"
                        )
                        ocr_cap_warned = True
                else:
                    ocr_attempts += 1
                    ocr_text = _ocr_page(work_path, idx, tmp_dir, warnings)
                    if ocr_text and len("".join(ocr_text.split())) > non_ws_len:
                        cleaned = ocr_text
                        ocr_used = True
                        ocr_pages.append(page_num)

            locator = f"page:{page_num}"
            anchor = f"<!-- page: {page_num} -->"
            page_md = f"{anchor}\n{cleaned}".strip() if cleaned else anchor
            sections.append(
                ExtractedSection(
                    locator=locator,
                    title=f"Page {page_num}",
                    ordinal=page_num,
                    markdown=page_md,
                    ocr_used=ocr_used,
                )
            )
            md_blocks.append(page_md)

    return ExtractedDocument(
        converter_id=CONVERTER_ID,
        converter_version=CONVERTER_VERSION,
        locator_kind="page",
        markdown="\n\n".join(md_blocks).strip() + "\n",
        sections=sections,
        page_count=len(sections),
        ocr_pages=ocr_pages,
        warnings=warnings,
    )
