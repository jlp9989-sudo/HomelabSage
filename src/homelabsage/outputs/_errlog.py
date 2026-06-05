"""Shared exception sanitiser for output failure logs.

Why this lives outside the redact module: redaction there is for
user-facing content (LLM input, Notion writes, etc) — substring
walks over arbitrary trees. This is the much narrower "summarise
an httpx / generic exception to a single line that NEVER includes
the request URL".

Why it matters: `httpx.HTTPStatusError.__str__()` and `httpx.HTTPError`
repr include the full request URL. The push outputs embed the auth
token IN the URL (Telegram `/bot{token}/sendMessage`, Gotify
`?token=`, Slack/Discord webhook secrets, Pushover, Apprise). A
single 4xx with default log routing → bot token in the log file,
in syslog-mcp, logspout, restic backups to Infomaniak. Use this
helper anywhere we catch an output-channel exception.
"""

from __future__ import annotations


def safe_error(e: BaseException) -> str:
    """Return a short, non-leaking representation of `e`.

    Format: `ExceptionType` or `ExceptionType (status N)`. Never
    includes the request URL, request body, response body, or
    `str(e)` directly.

    Behaviour by exception type:
      - `httpx.HTTPStatusError` (and httpx.HTTPError subtypes) → strip
        URL, surface only status code when present.
      - Other `httpx.HTTPError` (timeouts, connect errors) → just type.
      - Anything else → just type.

    Implementation note: we identify httpx errors by attribute presence
    rather than by import, so missing the lazy httpx dep doesn't break
    other callers.
    """
    cls = type(e).__name__
    resp = getattr(e, "response", None)
    status = getattr(resp, "status_code", None) if resp is not None else None
    if status is not None:
        return f"{cls} (status {status})"
    return cls
