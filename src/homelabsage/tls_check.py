"""TLS cert expiry detector.

A self-hosted reverse proxy with a 90-day Let's Encrypt cert is
silently five days from breaking the whole homelab if `certbot renew`
stopped working. This module probes a list of URLs, reads the TLS
cert chain, and flags certs whose `notAfter` is within `warn_days`.

Pure stdlib `ssl` + `socket` — no `cryptography` or 3rd-party dep.
We connect, hand off to `ssl.create_default_context().wrap_socket()`,
read the peer cert, parse the expiry. Failure modes — connection
refused, name resolution, cert chain invalid — all return a single
failed `TLSCheck` with the reason.
"""

from __future__ import annotations

import logging
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from ._time import utcnow

log = logging.getLogger(__name__)


@dataclass
class TLSCheck:
    """One URL's verdict."""

    url: str
    ok: bool
    reason: str
    days_until_expiry: int | None = None
    not_after: str | None = None    # ISO
    issuer: str | None = None
    severity: str = "info"

    def to_context(self) -> dict:
        return {
            "url": self.url,
            "ok": self.ok,
            "reason": self.reason,
            "days_until_expiry": self.days_until_expiry,
            "not_after": self.not_after,
            "issuer": self.issuer,
            "severity": self.severity,
        }


def _severity(days: int | None, warn_days: int) -> str:
    """Map days-until-expiry to a severity bucket.

    None (probe failure) → high. Negative (already expired) → critical.
    """
    if days is None:
        return "high"
    if days <= 0:
        return "critical"
    if days <= warn_days // 3:
        return "high"
    if days <= warn_days:
        return "medium"
    return "info"


def _parse_x509_date(s: str) -> datetime | None:
    """Parse openssl's `Jun 15 12:00:00 2026 GMT` format."""
    if not s:
        return None
    try:
        # `%b %d %H:%M:%S %Y %Z` — `%Z` accepts GMT/UTC
        dt = datetime.strptime(s, "%b %d %H:%M:%S %Y %Z")
        from datetime import UTC
        return dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _issuer_summary(cert: dict) -> str:
    """`(O=...) CN=...` compact issuer line."""
    issuer = cert.get("issuer") or ()
    o = ""
    cn = ""
    for rdn in issuer:
        for k, v in rdn:
            if k == "commonName":
                cn = v
            elif k == "organizationName":
                o = v
    if o and cn:
        return f"({o}) {cn}"
    return cn or o or "(unknown issuer)"


def probe(
    url: str,
    *,
    timeout: float = 10.0,
    warn_days: int = 30,
    now: datetime | None = None,
) -> TLSCheck:
    """Probe a single URL. Never raises — failures return ok=False.

    Accepts http:// (skipped — no TLS), https://, or bare `host[:port]`
    (treated as https://). The scheme stripping is for the user's
    convenience because copy/pasting hostnames is easier than URLs.
    """
    raw_url = url.strip()
    if not raw_url:
        return TLSCheck(
            url=url, ok=False, reason="empty URL", severity="info",
        )
    # Default to https when the user gave a bare hostname.
    if "://" not in raw_url:
        raw_url = "https://" + raw_url
    parsed = urlparse(raw_url)
    if parsed.scheme == "http":
        return TLSCheck(
            url=url, ok=True, reason="http (no TLS)", severity="info",
        )
    if parsed.scheme != "https":
        return TLSCheck(
            url=url, ok=False,
            reason=f"unsupported scheme: {parsed.scheme}",
            severity="info",
        )
    host = parsed.hostname or ""
    port = parsed.port or 443
    if not host:
        return TLSCheck(url=url, ok=False, reason="missing host",
                        severity="info")
    try:
        ctx = ssl.create_default_context()
        with (
            socket.create_connection((host, port), timeout=timeout) as sock,
            ctx.wrap_socket(sock, server_hostname=host) as ss,
        ):
            cert = ss.getpeercert()
    except (TimeoutError, socket.gaierror, ConnectionError) as e:
        return TLSCheck(
            url=url, ok=False,
            reason=f"connect: {type(e).__name__}: {e}",
            severity="high",
        )
    except ssl.SSLError as e:
        return TLSCheck(
            url=url, ok=False, reason=f"tls: {e}", severity="high",
        )
    except OSError as e:
        return TLSCheck(
            url=url, ok=False, reason=f"os: {e}", severity="high",
        )

    not_after_raw = cert.get("notAfter") if isinstance(cert, dict) else None
    # `getpeercert()` returns `notAfter` as a str, but the dict-value
    # union also covers tuple-of-tuples (issuer/subject). Narrow.
    not_after = _parse_x509_date(
        not_after_raw if isinstance(not_after_raw, str) else "",
    )
    if not_after is None:
        return TLSCheck(
            url=url, ok=False, reason="couldn't parse cert expiry",
            severity="high",
        )
    end = now or utcnow()
    if end.tzinfo is None:
        from datetime import UTC
        end = end.replace(tzinfo=UTC)
    delta = not_after - end
    days = int(delta.total_seconds() // 86400)
    sev = _severity(days, warn_days)
    return TLSCheck(
        url=url, ok=days > 0, reason="ok" if days > 0 else "expired",
        days_until_expiry=days,
        not_after=not_after.isoformat(),
        issuer=_issuer_summary(cert) if isinstance(cert, dict) else None,
        severity=sev,
    )


def check_urls(
    urls: list[str],
    *,
    timeout: float = 10.0,
    warn_days: int = 30,
) -> list[TLSCheck]:
    """Probe each URL; bound total runtime by per-URL timeout."""
    return [probe(u, timeout=timeout, warn_days=warn_days) for u in urls]


__all__ = ["TLSCheck", "check_urls", "probe"]
