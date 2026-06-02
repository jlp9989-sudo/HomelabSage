"""Weekly digest — one rollup message summarising the last N days of scans.

Rationale: per-update notifications are good for "something needs my
attention RIGHT NOW" but they bury the slower-burn signals — orphans piling
up, the same `stale` repo flagged every day for a month, three breaking
changes that landed on the same Sunday and got lost in the per-item stream.

The digest exists to surface those. It runs on its own cron entry (Sundays
09:00 by default) so users can keep a daily scan + weekly digest without
either drowning out the other.

Output shape:
  - The digest body is built once as Markdown.
  - It's then dispatched to whichever notification channels the user has
    enabled for the digest (any subset of the existing outputs: Telegram,
    Discord, Ntfy, Gotify — Notion is per-row by design and excluded).
  - The digest body is ALSO written to `<notes_dir>/digest.md` when notes
    are configured, so the curator picks it up as context on the next pass
    (recursive context — last week's misses inform this week's analyses).

The digest never invents content. Every line cites a concrete row from the
DB; the LLM is not in the loop here on purpose (we'd be summarising
summaries — opportunity for hallucination with no upside).

Interaction with the parity gate (`parity_gate.enabled`):
  - Per-update push notifications honour the gate (see `engine.run_once`).
  - This digest does NOT honour the gate by default — it's the backstop
    for missed real-time pings, so silencing it during parity would leave
    no signal at all.
  - Users who want strict silence flip `parity_gate.skip_digest_too =
    true`; the scheduler hook in `web.lifecycle._digest_job` then
    re-checks the gate before firing.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from ._time import utcnow
from .config import Config
from .db import Database
from .models import AnalyzedUpdate, UpdateStatus

log = logging.getLogger(__name__)


_SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "info": "🔵",
}


@dataclass
class DigestStats:
    """What the digest reports — a frozen snapshot of the lookback window."""

    period_start: datetime
    period_end: datetime
    counts_by_severity: dict[str, int]
    new_total: int
    analyzed_total: int
    top_items: list[AnalyzedUpdate]
    orphan_items: list[AnalyzedUpdate]
    abandoned_repos: list[AnalyzedUpdate]


def collect_stats(
    db: Database,
    *,
    days: int = 7,
    now: datetime | None = None,
    top_n: int = 8,
) -> DigestStats:
    """Roll up the last `days` of stored updates into one DigestStats.

    The DB stores `detected_at` in UTC; we compare in UTC so DST shifts
    don't bleed an item in or out by an hour. Callers passing a naive
    `now` (older tests, programmatic uses) get it upgraded to tz-aware
    UTC silently — the alternative (raising) breaks the long tail of
    code that doesn't yet know about the tz-aware migration.
    """
    if now is None:
        now = utcnow()
    elif now.tzinfo is None:
        from datetime import UTC
        now = now.replace(tzinfo=UTC)
    cutoff = now - timedelta(days=days)
    # Pull a generous page and filter in Python — the DB list() API is
    # ordered by detected_at DESC, so a 500-row cap is plenty for a week
    # even on the busiest homelabs.
    items = [it for it in db.list(limit=500) if it.detected_at >= cutoff]

    counts: Counter[str] = Counter()
    for it in items:
        if it.analysis:
            counts[it.analysis.severity.value] += 1

    # Top items: highest severity first, then most recent.
    def _key(it: AnalyzedUpdate) -> tuple[int, datetime]:
        sev = it.analysis.severity.order if it.analysis else -1
        return (sev, it.detected_at)

    top = sorted(items, key=_key, reverse=True)[:top_n]

    orphans = [
        it for it in items
        if isinstance(it.update.context, dict)
        and it.update.context.get("orphan_since_days")
    ]
    abandoned = [
        it for it in items
        if isinstance(it.update.context, dict)
        and (it.update.context.get("repo_health") or {}).get("status") == "abandoned"
    ]
    return DigestStats(
        period_start=cutoff,
        period_end=now,
        counts_by_severity=dict(counts),
        new_total=sum(1 for it in items if it.status == UpdateStatus.NEW),
        analyzed_total=sum(1 for it in items if it.status == UpdateStatus.ANALYZED),
        top_items=top,
        orphan_items=orphans,
        abandoned_repos=abandoned,
    )


def render_markdown(stats: DigestStats, *, public_ui_url: str = "") -> str:
    """Render `DigestStats` as a Markdown body suitable for any channel.

    The format is intentionally plain — no HTML, no Discord-specific
    embed syntax — so the same text round-trips through Telegram /
    Discord / Ntfy / Gotify / `notes/digest.md` without per-channel
    rewriting.
    """
    period = (
        f"{stats.period_start.strftime('%Y-%m-%d')} → "
        f"{stats.period_end.strftime('%Y-%m-%d')}"
    )
    lines: list[str] = [
        f"# HomelabSage digest — {stats.period_end.strftime('%Y-%m-%d')}",
        f"_Window: {period} ({(stats.period_end - stats.period_start).days} days)_",
        "",
    ]

    if not (stats.counts_by_severity or stats.top_items):
        lines.append("Nothing detected this week. All quiet.")
        if public_ui_url:
            lines.append(f"\n[Open HomelabSage]({public_ui_url})")
        return "\n".join(lines)

    # Counts line — drop zero buckets so the summary stays tight.
    parts = []
    for sev in ("critical", "high", "medium", "info"):
        n = stats.counts_by_severity.get(sev, 0)
        if n:
            parts.append(f"{_SEVERITY_EMOJI[sev]} {n} {sev}")
    if parts:
        lines.append("**Severity:** " + " · ".join(parts))
    lines.append(
        f"**Activity:** {stats.new_total} new, "
        f"{stats.analyzed_total} analyzed."
    )
    lines.append("")

    if stats.top_items:
        lines.append("## Highlights")
        for it in stats.top_items:
            u = it.update
            a = it.analysis
            sev = a.severity.value if a else "info"
            emoji = _SEVERITY_EMOJI[sev]
            head = (
                f"{emoji} **{u.subject}** "
                f"`{u.current_version}` → `{u.new_version}`"
            )
            if u.release_url:
                head += f" — [release notes]({u.release_url})"
            lines.append(f"- {head}")
            if a and a.summary:
                lines.append(f"  - {a.summary}")
            if a and a.recommended_action:
                lines.append(f"  - _Action:_ {a.recommended_action}")

    if stats.orphan_items:
        lines.append("")
        lines.append("## Orphans (exited containers with pending updates)")
        for it in stats.orphan_items:
            ctx = it.update.context if isinstance(it.update.context, dict) else {}
            days = ctx.get("orphan_since_days")
            lines.append(
                f"- **{it.update.subject}** — stopped {days} days ago "
                f"({it.update.current_version} → {it.update.new_version})"
            )

    if stats.abandoned_repos:
        lines.append("")
        lines.append("## Abandoned upstreams")
        for it in stats.abandoned_repos:
            ctx = it.update.context if isinstance(it.update.context, dict) else {}
            rh = ctx.get("repo_health", {}) or {}
            reason = rh.get("reason") or "no recent activity"
            lines.append(
                f"- **{it.update.subject}** — upstream {reason}; "
                f"evaluate a maintained fork before upgrading"
            )

    if public_ui_url:
        lines.append("")
        lines.append(f"[Open HomelabSage]({public_ui_url})")
    return "\n".join(lines)


# ─── Dispatchers — one per channel, kept small + dependency-free ────────

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
        log.error("digest telegram failed: %s", e)


async def _send_via_discord(cfg, body: str) -> None:
    if not cfg.enabled or not cfg.webhook_url:
        return
    # Discord truncates content at 2000 chars; long digests degrade to a
    # single embed description (limit 4096) which gives us breathing room.
    payload = {
        "embeds": [{
            "title": "HomelabSage weekly digest",
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
        log.error("digest discord failed: %s", e)


async def _send_via_ntfy(cfg, body: str) -> None:
    if not cfg.enabled or not cfg.server_url or not cfg.topic:
        return
    url = f"{cfg.server_url.rstrip('/')}/{cfg.topic.lstrip('/')}"
    headers = {
        "Title": "HomelabSage weekly digest",
        "Tags": "newspaper",
        "Priority": "3",
    }
    if cfg.auth_token:
        headers["Authorization"] = f"Bearer {cfg.auth_token}"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, content=body.encode("utf-8"), headers=headers)
            r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("digest ntfy failed: %s", e)


async def _send_via_gotify(cfg, body: str) -> None:
    if not cfg.enabled or not cfg.server_url or not cfg.token:
        return
    url = f"{cfg.server_url.rstrip('/')}/message"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                url,
                params={"token": cfg.token},
                json={
                    "title": "HomelabSage weekly digest",
                    "message": body,
                    "priority": 3,
                },
            )
            r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("digest gotify failed: %s", e)


_CHANNEL_DISPATCH = {
    "telegram": ("outputs.telegram", _send_via_telegram),
    "discord":  ("outputs.discord",  _send_via_discord),
    "ntfy":     ("outputs.ntfy",     _send_via_ntfy),
    "gotify":   ("outputs.gotify",   _send_via_gotify),
}


async def deliver(cfg: Config, body: str, channels: Iterable[str]) -> dict[str, str]:
    """Push the digest body to every channel in `channels`.

    Returns a `{channel: status}` map for the CLI to print. Failures are
    logged but never raised — a broken Discord webhook must not prevent
    delivery to Telegram on the same run.
    """
    results: dict[str, str] = {}
    for name in channels:
        spec = _CHANNEL_DISPATCH.get(name)
        if spec is None:
            results[name] = "unknown channel"
            continue
        attr_path, sender = spec
        # Resolve `outputs.telegram` / `outputs.discord` / etc on the config.
        leaf = cfg
        try:
            for part in attr_path.split("."):
                leaf = getattr(leaf, part)
        except AttributeError:
            results[name] = "config missing"
            continue
        try:
            await sender(leaf, body)
        except Exception as e:  # last-ditch safety net; senders already log
            log.exception("digest channel %s crashed: %s", name, e)
            results[name] = f"error: {e}"
            continue
        if not getattr(leaf, "enabled", False):
            results[name] = "skipped (channel disabled)"
        else:
            results[name] = "sent"
    return results


def write_to_notes(body: str, notes_dir: str | Path) -> Path | None:
    """Pin the digest to `<notes_dir>/digest.md` so the curator sees it next pass.

    Returns the written path, or None when no notes_dir is configured. Atomic
    write (`.tmp` rename) so a partial write never leaves a half-file on disk.
    """
    if not notes_dir:
        return None
    out_dir = Path(notes_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("digest notes_dir mkdir failed: %s", e)
        return None
    target = out_dir / "digest.md"
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


async def run_digest(
    cfg: Config,
    *,
    days: int = 7,
    dry_run: bool = False,
    channels: Iterable[str] | None = None,
    now: datetime | None = None,
) -> tuple[str, dict[str, str], Path | None]:
    """End-to-end: collect → render → write notes → deliver.

    Returns `(body, channel_results, notes_path)`. The CLI uses all three;
    the scheduler hook ignores the body but logs the channel summary.
    """
    db = Database(cfg.storage.database_path)
    try:
        stats = collect_stats(db, days=days, now=now)
    finally:
        db.close()
    body = render_markdown(stats)

    if dry_run:
        return body, {}, None

    notes_path = write_to_notes(body, cfg.notes.notes_dir) if cfg.notes.notes_dir else None

    # Default channel set: every output the user has enabled (Notion
    # excluded — it's per-row, not a digest medium). The caller can
    # override with an explicit list, e.g. from a CLI flag.
    if channels is None:
        channels = [
            name for name in _CHANNEL_DISPATCH
            if getattr(getattr(cfg.outputs, name, None), "enabled", False)
        ]
    results = await deliver(cfg, body, list(channels))
    return body, results, notes_path


def run_digest_blocking(cfg: Config, **kw) -> tuple[str, dict[str, str], Path | None]:
    """Sync entrypoint for the CLI and the scheduler job."""
    return asyncio.run(run_digest(cfg, **kw))
