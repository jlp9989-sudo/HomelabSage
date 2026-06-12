"""Severity-aware notification batching.

When `outputs.batching.enabled` is true, push outputs (Telegram / Discord /
Ntfy / Gotify) accumulate items at severity ≤ `below_severity` during a
scan and emit a single rollup message at the end instead of one ping per
item. Critical / High keep firing immediately so urgent signals never get
delayed by the rollup.

The rollup body is short, deterministic Markdown — same shape as the
weekly digest, scoped to one scan. Dispatch goes through the same per-
channel senders the digest uses, so the message round-trips through every
push channel without per-channel rewrite.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from .._time import utcnow
from ..config import Config
from ..models import AnalyzedUpdate, Severity
from ._errlog import safe_error

log = logging.getLogger(__name__)


_SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "info": "🔵",
}


def below_threshold(item: AnalyzedUpdate, threshold: Severity) -> bool:
    """True iff the item's severity is STRICTLY below `threshold`.

    Items at or above the threshold dispatch immediately; items below
    accumulate into the batch. Items without an Analysis (LLM failed)
    are NOT batched — they're already silenced by the per-output gate.
    """
    if item.analysis is None:
        return False
    return item.analysis.severity.order < threshold.order


def render_batch(
    items: list[AnalyzedUpdate],
    *,
    now: datetime | None = None,
) -> str:
    """Compose one rollup body for `items`. Plain Markdown; channel-neutral."""
    now = now or utcnow()
    counts: dict[str, int] = {}
    for it in items:
        if it.analysis:
            sev = it.analysis.severity.value
            counts[sev] = counts.get(sev, 0) + 1

    head = f"# {len(items)} low-severity updates"
    pills = " · ".join(
        f"{_SEVERITY_EMOJI[s]} {n} {s}" for s, n in counts.items() if n
    )
    lines = [head, f"_{now.strftime('%Y-%m-%d %H:%M UTC')}_"]
    if pills:
        lines.append(f"**Severity:** {pills}")
    lines.append("")
    for it in items[:25]:    # cap so the body doesn't blow Telegram's 4K limit
        u = it.update
        a = it.analysis
        sev = a.severity.value if a else "info"
        emoji = _SEVERITY_EMOJI[sev]
        head = f"{emoji} **{u.subject}** `{u.current_version}` → `{u.new_version}`"
        lines.append(f"- {head}")
        if a and a.summary:
            lines.append(f"  - {a.summary}")
    if len(items) > 25:
        lines.append("")
        lines.append(f"_... {len(items) - 25} more truncated._")
    return "\n".join(lines)


# ─── per-channel senders (identical shape to digest) ────────────────────


async def _send_via_telegram(cfg, body: str) -> None:
    if not cfg.enabled or not cfg.bot_token or not cfg.chat_id:
        return
    url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, json={
                "chat_id": cfg.chat_id,
                "text": body,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            })
            r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("batch telegram failed: %s", safe_error(e))


async def _send_via_discord(cfg, body: str) -> None:
    if not cfg.enabled or not cfg.webhook_url:
        return
    payload: dict[str, Any] = {
        "embeds": [{
            "title": "HomelabSage — low-severity rollup",
            "description": body[:4000],
            "color": 0x3498DB,
        }],
        "username": cfg.username or "HomelabSage",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(cfg.webhook_url, json=payload)
            r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("batch discord failed: %s", safe_error(e))


async def _send_via_ntfy(cfg, body: str) -> None:
    if not cfg.enabled or not cfg.server_url or not cfg.topic:
        return
    url = f"{cfg.server_url.rstrip('/')}/{cfg.topic.lstrip('/')}"
    headers = {
        "Title": "HomelabSage — low-severity rollup",
        "Tags": "bell",
        "Priority": "3",
    }
    if cfg.auth_token:
        headers["Authorization"] = f"Bearer {cfg.auth_token}"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, content=body.encode("utf-8"), headers=headers)
            r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("batch ntfy failed: %s", safe_error(e))


async def _send_via_gotify(cfg, body: str) -> None:
    if not cfg.enabled or not cfg.server_url or not cfg.token:
        return
    url = f"{cfg.server_url.rstrip('/')}/message"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, params={"token": cfg.token}, json={
                "title": "HomelabSage — low-severity rollup",
                "message": body,
                "priority": 3,
            })
            r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("batch gotify failed: %s", safe_error(e))


_DISPATCH = {
    "telegram": _send_via_telegram,
    "discord":  _send_via_discord,
    "ntfy":     _send_via_ntfy,
    "gotify":   _send_via_gotify,
}


def _channel_items(
    sub: Any, items: list[AnalyzedUpdate],
) -> list[AnalyzedUpdate]:
    """Filter the batch down to what this channel would accept singly.

    A channel with `min_severity: high` explicitly asked not to be pinged
    below high — a rollup of info/medium items through the back door
    violates that. Items without an analysis never reach the batch
    (`below_threshold` excludes them), so missing-analysis is just a
    defensive skip here.
    """
    floor = Severity(getattr(sub, "min_severity", "info"))
    return [
        it for it in items
        if it.analysis is not None
        and it.analysis.severity.order >= floor.order
    ]


def _channel_quiet(sub: Any) -> bool:
    """True iff this channel's quiet-hours window is active right now.

    No bypass check: batched items are below the batch threshold by
    definition, so they can never meet the (≥ critical-ish) bypass floor.
    """
    spec = getattr(sub, "quiet_hours", "")
    if not spec:
        return False
    from ..quiet_hours import is_quiet_now
    return is_quiet_now(
        window_spec=spec,
        timezone_name=getattr(sub, "quiet_hours_timezone", "UTC"),
    ).quiet


async def dispatch_batch(cfg: Config, items: list[AnalyzedUpdate]) -> dict[str, str]:
    """Emit one rollup per enabled push channel. Returns `{channel: status}`.

    Each channel gets its own cut of the batch: items below the channel's
    `min_severity` are dropped, and a channel inside its quiet-hours
    window is skipped outright (not queued — the rollup is trivia by
    definition and the weekly digest is the backstop).

    No-op when `items` is empty (caller's check) or when batching is
    disabled in config. Safe to call from the engine at end-of-scan.
    """
    if not items:
        return {}
    results: dict[str, str] = {}
    for name, sender in _DISPATCH.items():
        sub = getattr(cfg.outputs, name, None)
        if sub is None:
            continue
        if not getattr(sub, "enabled", False):
            continue
        chan_items = _channel_items(sub, items)
        if not chan_items:
            results[name] = "skipped: all items below min_severity"
            continue
        if _channel_quiet(sub):
            results[name] = "skipped: quiet hours"
            continue
        try:
            await sender(sub, render_batch(chan_items))
            results[name] = "sent"
        except Exception as e:  # last-ditch safety net
            log.exception("batch dispatch %s crashed", name)
            results[name] = f"error: {e}"
    return results
