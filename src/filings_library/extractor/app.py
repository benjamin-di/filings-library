"""Stateless Starlette extractor HTTP service (`POST /extract`, `GET /health`) and client."""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from filings_library.extractor.html_ixbrl import extract_html_ixbrl
from filings_library.extractor.pdf_extract import EncryptedPdfError, extract_pdf
from filings_library.extractor.xlsx_extract import extract_xlsx
from filings_library.models import ExtractedDocument

MAX_EXTRACT_BYTES = 250 * 1024 * 1024  # 250 MiB


def _detect_kind(mime_type: str, filename: str) -> str | None:
    mime = mime_type.split(";", 1)[0].strip().lower()
    fname = filename.strip().lower()
    if mime in {"text/html", "application/xhtml+xml"} or fname.endswith(
        (".htm", ".html", ".xhtml")
    ):
        return "html"
    if mime == "application/pdf" or fname.endswith(".pdf"):
        return "pdf"
    if (
        mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        or fname.endswith(".xlsx")
    ):
        return "xlsx"
    return None


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "capabilities": {
                "qpdf": shutil.which("qpdf") is not None,
                "pdftotext": shutil.which("pdftotext") is not None,
                "tesseract": shutil.which("tesseract") is not None,
                "pypdfium2": importlib.util.find_spec("pypdfium2") is not None,
            },
        }
    )


async def _extract(request: Request) -> JSONResponse:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length.strip())
        except ValueError:
            declared_size = -1
        if declared_size > MAX_EXTRACT_BYTES:
            return JSONResponse(
                {"error": "payload_too_large", "detail": f"Max size is {MAX_EXTRACT_BYTES} bytes"},
                status_code=413,
            )

    buf = bytearray()
    async for chunk in request.stream():
        if len(buf) + len(chunk) > MAX_EXTRACT_BYTES:
            return JSONResponse(
                {"error": "payload_too_large", "detail": f"Max size is {MAX_EXTRACT_BYTES} bytes"},
                status_code=413,
            )
        buf.extend(chunk)
    body = bytes(buf)

    if not body:
        return JSONResponse(
            {"error": "empty_body", "detail": "Request body must not be empty"},
            status_code=400,
        )

    mime_type = request.query_params.get("mime_type") or request.headers.get("content-type", "")
    filename = request.query_params.get("filename") or request.headers.get("x-filename", "")
    kind = _detect_kind(mime_type, filename)
    if kind is None:
        return JSONResponse(
            {
                "error": "unsupported_media_type",
                "detail": f"Unsupported mime_type={mime_type!r} filename={filename!r}",
            },
            status_code=415,
        )

    try:
        if kind == "html":
            extracted = await asyncio.to_thread(extract_html_ixbrl, body)
        elif kind == "pdf":
            extracted = await asyncio.to_thread(extract_pdf, body)
        else:
            extracted = await asyncio.to_thread(extract_xlsx, body)
    except EncryptedPdfError as exc:
        return JSONResponse(
            {"error": "encrypted_pdf", "detail": str(exc)},
            status_code=422,
        )
    except Exception as exc:
        return JSONResponse(
            {"error": "extraction_failed", "detail": str(exc)},
            status_code=422,
        )

    return JSONResponse(extracted.model_dump())


def create_extractor_app() -> Starlette:
    """Create the stateless Starlette ASGI app for document extraction."""
    return Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            Route("/extract", _extract, methods=["POST"]),
        ]
    )


class ExtractorClient:
    """Async client for the stateless extractor service (HTTP URL or ASGI transport)."""

    def __init__(
        self,
        *,
        base_url: str = "http://extractor.local",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout

    @classmethod
    def in_process(cls) -> ExtractorClient:
        return cls(
            base_url="http://extractor.local",
            transport=httpx.ASGITransport(app=create_extractor_app()),
        )

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self._base_url, transport=self._transport, timeout=self._timeout
        ) as client:
            resp = await client.get("/health")
            resp.raise_for_status()
            return dict(resp.json())

    async def extract(
        self, data: bytes, *, mime_type: str, filename: str = ""
    ) -> ExtractedDocument:
        async with httpx.AsyncClient(
            base_url=self._base_url, transport=self._transport, timeout=self._timeout
        ) as client:
            resp = await client.post(
                "/extract",
                params={"mime_type": mime_type, "filename": filename},
                headers={"Content-Type": "application/octet-stream"},
                content=data,
            )
            if resp.status_code == 422:
                payload = resp.json()
                if payload.get("error") == "encrypted_pdf":
                    raise EncryptedPdfError(payload.get("detail", "Encrypted PDF"))
                raise ValueError(payload.get("detail", "Extraction failed"))
            resp.raise_for_status()
            return ExtractedDocument.model_validate(resp.json())
