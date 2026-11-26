"""Fail-closed client for `GET /internal/library-targets` (schema=1)."""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import ValidationError

from filings_library.models import LibraryTargetsPayload

MAX_TTL_SECONDS = 60.0
DEFAULT_TOKEN_ENV = "FILINGS_TARGETS_TOKEN"


class HubUnavailableError(RuntimeError):
    """Raised when the upstream targets hub is unavailable or returns an invalid payload."""


class LibraryTargetsClient:
    """Fetches `LibraryTargetsPayload` with TTL <= 60s and strict fail-closed semantics.

    Never returns stale holdings after TTL expiry or on any upstream error.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        token_file: Path | str | None = None,
        ttl_seconds: float = 60.0,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0 or ttl_seconds > MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be in [0, {MAX_TTL_SECONDS}], got {ttl_seconds}")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._token_file = Path(token_file) if token_file is not None else None
        self._ttl_seconds = float(ttl_seconds)
        self._timeout = timeout
        self._transport = transport
        self._clock = clock
        self._cached_payload: LibraryTargetsPayload | None = None
        self._cached_at: float | None = None

    def _resolve_token(self) -> str:
        if self._token:
            return self._token.strip()
        if self._token_file is not None:
            mode = self._token_file.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise PermissionError(
                    f"Bearer token file must be 0600 (no group/other access): {self._token_file}"
                )
            token = self._token_file.read_text(encoding="utf-8").strip()
            if not token:
                raise ValueError(f"Bearer token file is empty: {self._token_file}")
            return token
        env_token = os.environ.get(DEFAULT_TOKEN_ENV, "").strip()
        if not env_token:
            raise HubUnavailableError(
                f"No targets bearer token configured (set token, token_file, or {DEFAULT_TOKEN_ENV})"
            )
        return env_token

    def invalidate_cache(self) -> None:
        self._cached_payload = None
        self._cached_at = None

    async def fetch_targets(self, *, force_refresh: bool = False) -> LibraryTargetsPayload:
        now = self._clock()
        if (
            not force_refresh
            and self._cached_payload is not None
            and self._cached_at is not None
            and (now - self._cached_at) < self._ttl_seconds
        ):
            return self._cached_payload

        # Always clear stale cache before attempting upstream refresh so we fail closed.
        self.invalidate_cache()

        token = self._resolve_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                resp = await client.get("/internal/library-targets", headers=headers)
        except httpx.HTTPError as exc:
            raise HubUnavailableError(f"Targets hub request failed: {exc}") from exc

        if resp.status_code != 200:
            raise HubUnavailableError(
                f"Targets hub returned HTTP {resp.status_code} for /internal/library-targets"
            )

        try:
            raw = resp.json()
            payload = LibraryTargetsPayload.model_validate(raw)
        except (ValueError, ValidationError) as exc:
            raise HubUnavailableError(f"Invalid targets payload from hub: {exc}") from exc

        if payload.schema_version != 1:
            raise HubUnavailableError(
                f"Unsupported targets payload schema version: {payload.schema_version}"
            )

        self._cached_payload = payload
        self._cached_at = self._clock()
        return payload

    async def allowed_source_keys_for(self, person: str) -> set[str]:
        """Return the allowed `source_keys` for `person`, failing closed if hub is unavailable."""
        payload = await self.fetch_targets()
        for scope in payload.scopes:
            if scope.person == person:
                return set(scope.source_keys)
        return set()
