"""SSRF-safe public HTTPS URL validation and host allowlisting."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Collection, Sequence
from urllib.parse import urlsplit

SEC_ALLOWED_HOSTS: frozenset[str] = frozenset({"data.sec.gov", "www.sec.gov", "sec.gov"})
HKEX_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {"www1.hkexnews.hk", "www.hkexnews.hk", "hkexnews.hk"}
)
ESEF_ALLOWED_HOSTS: frozenset[str] = frozenset({"filings.xbrl.org"})

HostResolver = Callable[[str, int], Sequence[str]]


class UnsafeUrlError(ValueError):
    """Raised when a URL fails HTTPS, host allowlist, or public-IP SSRF checks."""


def _default_resolve_host(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeUrlError(f"DNS resolution failed for host {host!r}: {exc}") from exc
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr:
            ips.append(str(sockaddr[0]))
    if not ips:
        raise UnsafeUrlError(f"DNS resolution returned no addresses for host {host!r}")
    return ips


def _assert_public_ip(raw_ip: str, *, host: str) -> None:
    cleaned = raw_ip.split("%", 1)[0].strip()
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError as exc:
        raise UnsafeUrlError(f"Invalid resolved IP {raw_ip!r} for host {host!r}") from exc

    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        candidates.append(ip.ipv4_mapped)

    for candidate in candidates:
        if (
            candidate.is_private
            or candidate.is_loopback
            or candidate.is_link_local
            or candidate.is_reserved
            or candidate.is_multicast
            or candidate.is_unspecified
        ):
            raise UnsafeUrlError(f"Blocked non-public IP address {candidate} for host {host!r}")


def validate_public_https_url(
    url: str,
    *,
    allowed_hosts: Collection[str] | None = None,
    allow_http_in_tests: bool = False,
    allowed_ports: Collection[int] = (443,),
    resolver: HostResolver | None = None,
) -> str:
    """Validate that `url` is a public HTTPS URL (no credentials, no private/loopback IPs)."""
    if not isinstance(url, str):
        raise UnsafeUrlError("URL must be a string")
    cleaned_url = url.strip()
    if not cleaned_url or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned_url):
        raise UnsafeUrlError("URL is empty or contains control characters")

    parsed = urlsplit(cleaned_url)
    scheme = (parsed.scheme or "").lower()
    allowed_schemes = {"https", "http"} if allow_http_in_tests else {"https"}
    if scheme not in allowed_schemes:
        raise UnsafeUrlError(f"Disallowed URL scheme {scheme!r}; expected https")

    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URLs with embedded credentials are not allowed")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or any(ch.isspace() for ch in host):
        raise UnsafeUrlError("URL must include a valid hostname")

    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError(f"Invalid port in URL: {exc}") from exc

    effective_port = (
        explicit_port if explicit_port is not None else (80 if scheme == "http" else 443)
    )
    valid_ports = set(allowed_ports)
    if allow_http_in_tests and scheme == "http":
        valid_ports.add(80)
    if effective_port not in valid_ports:
        raise UnsafeUrlError(f"Disallowed port {effective_port} in URL")

    if allowed_hosts is not None:
        normalized_allowlist = {h.strip().lower().rstrip(".") for h in allowed_hosts if h.strip()}
        if host not in normalized_allowlist:
            raise UnsafeUrlError(f"Host {host!r} is not in the allowed host list")

    # Check literal IP first; otherwise resolve via DNS / injected resolver.
    is_literal_ip = False
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False

    if is_literal_ip:
        _assert_public_ip(host, host=host)
    else:
        resolve_fn = resolver or _default_resolve_host
        ips = list(resolve_fn(host, effective_port))
        if not ips:
            raise UnsafeUrlError(f"Host {host!r} resolved to no IP addresses")
        for ip_str in ips:
            _assert_public_ip(ip_str, host=host)

    return cleaned_url
