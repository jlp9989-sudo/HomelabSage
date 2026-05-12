"""Pure redaction helpers for `homelabsage export --redact`.

Goal: turn a HomelabSage snapshot (container inventory + recent analyses) into
something the user can paste into a public GitHub issue without leaking IPs,
hostnames, or credentials.

Design notes:

- All public functions are pure (no I/O, no globals). The CLI wraps them and
  walks the snapshot once.
- The redactor errs on the side of over-redaction. Missing an over-redaction
  is harmless ("why is this `host-3`?"); missing an under-redaction can leak
  a token.
- Replacements are *stable per snapshot*: the third unique IP seen is always
  `10.0.0.3` within the same export. That preserves the relationship between
  entries (two services pointing at the same host stay visibly co-located).
- Replacements are NOT stable across exports — each call to `Sanitiser()`
  starts a fresh mapping. Don't use the redacted output as a long-term id.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

# Substrings that, if found in an environment-variable key, force the value
# to be redacted. The check is case-insensitive; matches anywhere in the key.
SECRET_KEY_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "auth",
    "credential",
    "private",
    "session",
    "salt",
    "signing_key",
    "client_secret",
)

# Values that *look like* credentials are redacted even when the key name is
# innocuous. These cover the common shapes — long hex, JWT, common-format
# Bearer tokens. We intentionally avoid generic "long string" rules; they
# false-positive on hashes the user wants to keep visible.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}$"),  # JWT
    re.compile(r"^gh[ps]_[A-Za-z0-9]{30,}$"),                                       # GitHub PAT
    re.compile(r"^sk-[A-Za-z0-9]{20,}$"),                                           # OpenAI-shaped
    re.compile(r"^xox[abprs]-[A-Za-z0-9-]{10,}$"),                                  # Slack
    re.compile(r"^Bearer\s+[A-Za-z0-9_.-]{20,}$"),                                  # raw bearer
)

# IP regex matches both v4 (with optional /CIDR) and v6 (kept simple — full
# v6 grammar is huge; we accept anything that has at least two colons and
# only valid characters, then validate with ipaddress to weed out false
# positives like `key:value` colons in URLs).
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b")
_IPV6_RE = re.compile(r"\b[0-9a-fA-F:]+:[0-9a-fA-F:]+\b")

# A hostname token is something that looks like a domain or short hostname:
# letters/digits/hyphens, at least one dot, no spaces. We deliberately do NOT
# match bare single-word hostnames (`tower`, `halo`) — too many false positives.
_HOSTNAME_RE = re.compile(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b")

# Hostnames we *never* redact (these are documentation that the redaction is
# trying to enable — leaking them adds noise without privacy gain).
_HOSTNAME_ALLOWLIST: frozenset[str] = frozenset(
    {
        "github.com",
        "api.github.com",
        "ghcr.io",
        "docker.io",
        "hub.docker.com",
        "openai.com",
        "api.openai.com",
        "anthropic.com",
        "console.anthropic.com",
        "api.groq.com",
        "openrouter.ai",
        "generativelanguage.googleapis.com",
        "linuxserver.io",
        "fleet.linuxserver.io",
        "raw.githubusercontent.com",
    }
)

# IPs we *never* redact — loopback, link-local, documentation ranges, broadcast.
_IP_ALLOWLIST_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("255.255.255.255/32"),
)


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    return any(marker in k for marker in SECRET_KEY_MARKERS)


def _looks_like_credential(value: str) -> bool:
    return any(p.match(value) for p in _VALUE_PATTERNS)


def _is_redactable_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str.split("/")[0])
    except ValueError:
        return False
    return not any(ip in net for net in _IP_ALLOWLIST_NETWORKS)


@dataclass
class Sanitiser:
    """Per-export state: holds the IP↔alias and hostname↔alias mappings.

    Construct one instance per export so aliases are stable within the dump
    but fresh between dumps.
    """

    _ip_map: dict[str, str] = field(default_factory=dict)
    _host_map: dict[str, str] = field(default_factory=dict)
    secret_placeholder: str = "<redacted>"

    def _ip_alias(self, ip: str) -> str:
        if ip not in self._ip_map:
            self._ip_map[ip] = f"10.0.0.{len(self._ip_map) + 1}"
        return self._ip_map[ip]

    def _host_alias(self, host: str) -> str:
        if host not in self._host_map:
            self._host_map[host] = f"host-{len(self._host_map) + 1}"
        return self._host_map[host]

    def sanitise_text(self, text: str) -> str:
        """Apply IP + hostname substitutions to a free-form string."""
        if not text:
            return text

        def _ip_sub(m: re.Match[str]) -> str:
            raw = m.group(0)
            if not _is_redactable_ip(raw):
                return raw
            return self._ip_alias(raw)

        def _ipv6_sub(m: re.Match[str]) -> str:
            raw = m.group(0)
            try:
                ipaddress.IPv6Address(raw)
            except ValueError:
                return raw
            return self._ip_alias(raw)

        def _host_sub(m: re.Match[str]) -> str:
            raw = m.group(0)
            if raw.lower() in _HOSTNAME_ALLOWLIST:
                return raw
            return self._host_alias(raw)

        out = _IPV4_RE.sub(_ip_sub, text)
        out = _IPV6_RE.sub(_ipv6_sub, out)
        out = _HOSTNAME_RE.sub(_host_sub, out)
        return out

    def sanitise_env(self, env: dict[str, str]) -> dict[str, str]:
        """Redact env-var values whose key looks secret OR whose value matches
        a credential pattern. Keys themselves are kept (the *name* of a token
        env var is often part of the bug context)."""
        out: dict[str, str] = {}
        for k, v in env.items():
            if _is_secret_key(k) or (isinstance(v, str) and _looks_like_credential(v)):
                out[k] = self.secret_placeholder
            else:
                out[k] = self.sanitise_text(v) if isinstance(v, str) else v
        return out

    def sanitise(self, value: Any) -> Any:
        """Walk an arbitrary JSON-shaped value and sanitise it in place.

        Dicts whose keys look secret have their values redacted recursively.
        Strings get the IP/hostname substitutions.
        Lists are walked.
        Anything else is returned as-is.
        """
        if isinstance(value, dict):
            out_d: dict[str, Any] = {}
            for k, v in value.items():
                if isinstance(k, str) and _is_secret_key(k):
                    out_d[k] = self.secret_placeholder
                else:
                    out_d[k] = self.sanitise(v)
            return out_d
        if isinstance(value, list):
            return [self.sanitise(v) for v in value]
        if isinstance(value, str):
            if _looks_like_credential(value):
                return self.secret_placeholder
            return self.sanitise_text(value)
        return value
