"""Scoped FastMCP server and authenticated Starlette HTTP application."""

from __future__ import annotations

import contextvars
import hmac
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from fastmcp import FastMCP
from signin_window.oidc import BrowserAuth
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from filings_library.service import (
    FilingsService,
    clamp_limit,
    clamp_limit_per_company,
    clamp_max_chars,
)
from filings_library.targets_client import HubUnavailableError
from filings_library.web_api import WebAuthResolver, build_viewer_url, build_web_routes

MIN_BEARER_TOKEN_LENGTH = 32
SUBJECT_HEADER = "x-fleet-subject"

_current_subject_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "filings_current_subject", default=None
)
_hub_errors_var: contextvars.ContextVar[list[HubUnavailableError] | None] = contextvars.ContextVar(
    "filings_hub_errors", default=None
)

T = TypeVar("T")


def _require_subject() -> str:
    subject = _current_subject_var.get()
    if not subject:
        raise PermissionError("Missing authenticated subject")
    return subject


async def _call_service(coro: Awaitable[T]) -> T:
    """Await a service call, recording `HubUnavailableError` for HTTP 503 mapping."""
    try:
        return await coro
    except HubUnavailableError as exc:
        errors = _hub_errors_var.get()
        if errors is not None:
            errors.append(exc)
        raise


def create_mcp_server(
    service: FilingsService,
    *,
    public_base_url: str | None = None,
) -> FastMCP:
    """Create the FastMCP server exposing the 6 scoped filings tools."""
    mcp = FastMCP("filings-library")

    @mcp.tool()
    async def filings_list_companies() -> list[dict[str, Any]]:
        """List companies the caller is authorized to access."""
        person = _require_subject()
        return await _call_service(service.list_companies(person))

    @mcp.tool()
    async def filings_list_documents(
        source_key: str | None = None,
        form_family: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List filings documents within the caller's authorized companies."""
        person = _require_subject()
        return await _call_service(
            service.list_documents(
                person,
                source_key=source_key,
                form_family=form_family,
                since=since,
                limit=clamp_limit(limit),
            )
        )

    @mcp.tool()
    async def filings_search(
        query: str,
        source_key: str | None = None,
        form_family: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search filings across the caller's authorized companies and return citations."""
        person = _require_subject()
        results = await _call_service(
            service.search(
                person,
                query,
                source_key=source_key,
                form_family=form_family,
                since=since,
                limit=clamp_limit(limit),
            )
        )
        if public_base_url:
            for item in results:
                url = build_viewer_url(
                    public_base_url,
                    str(item["document_id"]),
                    locator=str(item.get("locator") or "") or None,
                    query=query,
                )
                if url:
                    item["viewer_url"] = url
        return results

    @mcp.tool()
    async def filings_read(
        document_id: str,
        start_locator: str | None = None,
        end_locator: str | None = None,
        max_chars: int = 24000,
    ) -> dict[str, Any]:
        """Read bounded sections of an authorized filing with strict citations."""
        person = _require_subject()
        result = await _call_service(
            service.read(
                person,
                document_id,
                start_locator=start_locator,
                end_locator=end_locator,
                max_chars=clamp_max_chars(max_chars),
            )
        )
        if public_base_url and isinstance(result, dict):
            url = build_viewer_url(
                public_base_url,
                document_id,
                locator=start_locator,
            )
            if url:
                result["viewer_url"] = url
        return result

    @mcp.tool()
    async def filings_facts(
        concepts: list[str],
        source_keys: list[str] | None = None,
        limit_per_company: int = 8,
    ) -> list[dict[str, Any]]:
        """Query cached SEC XBRL companyfacts across the caller's authorized companies."""
        person = _require_subject()
        return await _call_service(
            service.facts(
                person,
                concepts,
                source_keys=source_keys,
                limit_per_company=clamp_limit_per_company(limit_per_company),
            )
        )

    @mcp.tool()
    async def filings_status() -> dict[str, Any]:
        """Return per-issuer coverage report strictly for the caller's authorized companies."""
        person = _require_subject()
        return await _call_service(service.status(person))

    return mcp


def _extract_headers(scope: Scope, name: str) -> list[str]:
    target = name.lower().encode("latin-1")
    return [
        value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == target
    ]


def _inject_no_store(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    filtered = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
    filtered.append((b"cache-control", b"no-store"))
    return filtered


class _ScopedAuthMiddleware:
    """ASGI middleware enforcing bearer auth, `X-Fleet-Subject`, `Cache-Control`, and 503 mapping."""

    def __init__(self, app: ASGIApp, *, backend_bearer_token: str | None) -> None:
        self._app = app
        cleaned = (backend_bearer_token or "").strip()
        self._expected_token: str | None = (
            cleaned if len(cleaned) >= MIN_BEARER_TOKEN_LENGTH else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path != "/mcp" and not path.startswith("/mcp/"):
            await self._app(scope, receive, send)
            return

        # Fail closed if bearer token is unconfigured or shorter than 32 chars.
        if self._expected_token is None:
            resp = JSONResponse(
                {"error": "unauthorized", "detail": "Backend bearer token is not configured"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
            await resp(scope, receive, send)
            return

        auth_headers = _extract_headers(scope, "authorization")
        if len(auth_headers) > 1:
            resp = JSONResponse(
                {"error": "unauthorized", "detail": "Duplicate Authorization header"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
            await resp(scope, receive, send)
            return

        auth_header = (auth_headers[0] if auth_headers else "").strip()
        scheme, _, provided_token = auth_header.partition(" ")
        provided_token = provided_token.strip()
        if scheme.lower() != "bearer" or not provided_token:
            resp = JSONResponse(
                {"error": "unauthorized", "detail": "Missing or invalid Authorization header"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
            await resp(scope, receive, send)
            return

        if not hmac.compare_digest(
            provided_token.encode("utf-8"), self._expected_token.encode("utf-8")
        ):
            resp = JSONResponse(
                {"error": "unauthorized", "detail": "Invalid bearer token"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
            await resp(scope, receive, send)
            return

        subject_headers = _extract_headers(scope, SUBJECT_HEADER)
        if len(subject_headers) > 1:
            resp = JSONResponse(
                {"error": "forbidden", "detail": "Duplicate X-Fleet-Subject header"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
            await resp(scope, receive, send)
            return

        subject = (subject_headers[0] if subject_headers else "").strip()
        if not subject:
            resp = JSONResponse(
                {"error": "forbidden", "detail": "Missing X-Fleet-Subject header"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
            await resp(scope, receive, send)
            return

        subject_token = _current_subject_var.set(subject)
        hub_errors: list[HubUnavailableError] = []
        hub_err_token = _hub_errors_var.set(hub_errors)
        captured: list[Message] = []

        async def _capture_send(message: Message) -> None:
            captured.append(message)

        try:
            await self._app(scope, receive, _capture_send)
        except HubUnavailableError as exc:
            resp = JSONResponse(
                {"error": "hub_unavailable", "detail": str(exc)},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
            await resp(scope, receive, send)
            return
        finally:
            _current_subject_var.reset(subject_token)
            _hub_errors_var.reset(hub_err_token)

        if hub_errors:
            resp = JSONResponse(
                {"error": "hub_unavailable", "detail": str(hub_errors[0])},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
            await resp(scope, receive, send)
            return

        for msg in captured:
            if msg["type"] == "http.response.start":
                msg = dict(msg)
                msg["headers"] = _inject_no_store(list(msg.get("headers", [])))
            await send(msg)


def create_http_app(
    service: FilingsService,
    *,
    backend_bearer_token: str | None,
    allowed_hosts: Sequence[str] | None = None,
    browser_auth: BrowserAuth | None = None,
    web_auth_resolver: WebAuthResolver | None = None,
    public_base_url: str | None = None,
    static_dir: Path | None = None,
) -> Starlette:
    """Create the hosted Starlette HTTP/MCP + read-only Web Workspace application.

    - `GET /health` checks SQLite liveness (200/503), no auth required.
    - `/mcp` mounts FastMCP behind bearer auth + `X-Fleet-Subject` + `HubUnavailableError` -> 503.
    - `/`, `/d/{id}`, `/api/*`, `/connections/*` serve the read-only Web Workspace behind OIDC.
    - Protected by `TrustedHostMiddleware` and sets `Cache-Control: no-store`.
    """
    mcp = create_mcp_server(service, public_base_url=public_base_url)
    mcp_app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    async def _health(_request: Request) -> JSONResponse:
        ok = await service.health_check()
        return JSONResponse(
            {"status": "ok" if ok else "unhealthy"},
            status_code=200 if ok else 503,
            headers={"Cache-Control": "no-store"},
        )

    hosts = list(allowed_hosts) if allowed_hosts is not None else ["localhost", "127.0.0.1"]

    middleware: list[Middleware] = [
        Middleware(TrustedHostMiddleware, allowed_hosts=hosts),
    ]
    if browser_auth is not None:
        middleware.append(Middleware(browser_auth.middleware))
    middleware.append(
        Middleware(
            _ScopedAuthMiddleware,  # type: ignore[arg-type]
            backend_bearer_token=backend_bearer_token,
        )
    )

    routes: list[BaseRoute] = [
        Route("/health", _health, methods=["GET"]),
        *build_web_routes(
            service,
            browser_auth=browser_auth,
            web_auth_resolver=web_auth_resolver,
            public_base_url=public_base_url,
            static_dir=static_dir,
        ),
        Mount("/", app=mcp_app),
    ]

    return Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=mcp_app.lifespan,
    )


def get_subject_for_testing() -> Callable[[], str | None]:
    """Helper exposing current subject accessor for diagnostics."""
    return _current_subject_var.get
