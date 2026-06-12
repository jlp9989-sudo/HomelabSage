"""SSRF guard for fetching user-supplied URLs.

`analyse <url>` (CLI, `/api/analyse`, MCP `analyze_url`) fetches an
arbitrary URL the user/agent hands us and feeds the body to the LLM and
the dashboard. Without a guard that's a server-side request forgery
primitive: a URL like `http://169.254.169.254/…` (cloud metadata) or
`http://192.168.1.1/…` (the router admin page) would be fetched from
*inside* the homelab and its response surfaced. A redirect from a benign
public URL to an internal one bypasses a naive host check, so the guard
must re-validate every hop.

This module is the single enforcement point:
  - `check_url_public(url)` — scheme is http(s) AND the host resolves to
    ONLY public IPs (rejects RFC1918, loopback, link-local — incl. the
    metadata address — reserved, multicast, and IPv6 equivalents).
  - `fetch_text_guarded(url)` — follows redirects MANUALLY, running the
    check before each hop, so a 302 into the private range is blocked.

DNS resolution is blocking, so the check runs in a worker thread when
called from async code.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


class BlockedURLError(Exception):
    """Raised when a URL is rejected as unsafe to fetch."""


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally-routable unicast addresses.

    `is_private` already covers RFC1918 + unique-local IPv6; the rest
    close the well-known SSRF targets: loopback, link-local (incl.
    `169.254.169.254` cloud metadata), reserved, multicast, and the
    unspecified `0.0.0.0` / `::`.
    """
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def check_url_public(url: str) -> None:
    """Raise `BlockedURLError` unless `url` is safe to fetch.

    A literal-IP host is checked directly; a hostname is resolved and
    EVERY returned address must be public (a single private answer in a
    multi-A record is enough to reject — that's the DNS-rebinding shape).
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise BlockedURLError(f"scheme {parsed.scheme!r} not allowed")
    host = parsed.hostname  # strips port + IPv6 brackets, lowercased
    if not host:
        raise BlockedURLError("no host in URL")

    # Literal IP fast path — no DNS.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not _ip_is_public(ip):
            raise BlockedURLError(f"{host} is not a public address")
        return

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise BlockedURLError(f"{host} does not resolve") from e
    addrs = {str(info[4][0]) for info in infos}
    if not addrs:
        raise BlockedURLError(f"{host} resolved to nothing")
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])  # strip zone id
        except ValueError:
            raise BlockedURLError(f"{host} resolved to unparseable {addr!r}") from None
        if not _ip_is_public(ip):
            raise BlockedURLError(f"{host} resolves to non-public {addr}")


async def fetch_text_guarded(
    url: str,
    *,
    timeout: float = 20.0,
    max_redirects: int = 5,
    headers: dict[str, str] | None = None,
) -> str | None:
    """GET `url` as text with SSRF protection on every redirect hop.

    Returns the body on a 200, or None for any block / non-200 / too many
    redirects / network error. Redirects are followed manually so each
    `Location` is re-validated before we connect to it — `httpx`'s
    built-in `follow_redirects` would chase a 302 into the private range
    before we could check it.
    """
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(max_redirects + 1):
            try:
                await asyncio.to_thread(check_url_public, current)
            except BlockedURLError as e:
                log.warning("blocked URL fetch %s: %s", current, e)
                return None
            try:
                r = await client.get(current, headers=headers or {})
            except httpx.HTTPError as e:
                log.debug("guarded fetch %s failed: %s", current, e)
                return None
            if r.is_redirect:
                loc = r.headers.get("location")
                if not loc:
                    return None
                current = str(httpx.URL(current).join(loc))
                continue
            if r.status_code != 200:
                return None
            return r.text
    log.warning("guarded fetch %s: too many redirects", url)
    return None


__all__ = ["BlockedURLError", "check_url_public", "fetch_text_guarded"]
