"""Runtime knobs: scheduler cadence, weekly digest, parity gate.

Grouped because they're conceptually adjacent: all three answer the
question "when and under what conditions should HomelabSage talk to
external systems?".

Cross-cut: see `parity` ↔ `digest` interplay
  - When `parity_gate.enabled` is true, the engine skips per-update push
    notifications during a parity check.
  - When `parity_gate.skip_digest_too` is true, the weekly digest is also
    held back. When false (default), the digest still fires — it acts as
    the backstop for missed real-time pings.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class SchedulerConfig(BaseModel):
    enabled: bool = True
    cron: str = Field(
        "0 9 * * *",
        description=(
            "When to run an automatic scan. Pick a preset or set a custom "
            "5-field cron expression (`min hour day month weekday`)."
        ),
        json_schema_extra={"ui_widget": "cron"},
    )
    timezone: str = Field(
        "UTC",
        description="IANA timezone the cron runs against, e.g. `Europe/Madrid`, `America/New_York`.",
        json_schema_extra={"ui_widget": "timezone"},
    )
    heartbeat_url: str = Field(
        "",
        description=(
            "Optional URL pinged with GET after every successful scan, e.g. "
            "an Uptime Kuma push monitor. Leave empty to disable."
        ),
    )

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        """Reject typos at save time rather than at scheduler-start time.

        Uses stdlib `zoneinfo`, which reads the system tzdata. We declare
        the `tzdata` PyPI package as a dependency so minimal OS images
        without an OS-level tz database (Alpine, distroless, Windows)
        still resolve every IANA name including aliases like `US/Pacific`.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(
                f"unknown timezone {v!r} — must be a valid IANA name like "
                f"'Europe/Madrid', 'America/New_York', or 'UTC'"
            ) from e
        return v


class DigestConfig(BaseModel):
    """Weekly rollup posted to chosen notification channels.

    The digest runs on a SEPARATE cron from the scan so users can do daily
    scans + weekly digest without either drowning the other. Disabled by
    default — most installs want to tune notification volume before opting
    in to a second message stream.

    Interaction with `parity_gate`: by default the digest IGNORES the
    parity gate so it can act as a backstop for missed real-time pings.
    Set `parity_gate.skip_digest_too = true` to suppress the digest too
    while parity is running.
    """

    enabled: bool = False
    cron: str = Field(
        "0 9 * * 0",
        description=(
            "When to post the digest. Default: Sundays at 09:00. Uses the "
            "same timezone as `scheduler.timezone`."
        ),
        json_schema_extra={"ui_widget": "cron"},
    )
    lookback_days: int = Field(
        7,
        description="How many days of history the digest summarises (default: one week).",
    )
    channels: list[str] = Field(
        default_factory=list,
        description=(
            "Which notification channels receive the digest. Each entry must "
            "be one of: `telegram`, `discord`, `ntfy`, `gotify`. Leave empty "
            "to auto-pick every enabled channel."
        ),
    )

    @field_validator("channels")
    @classmethod
    def _check_channels(cls, v: list[str]) -> list[str]:
        allowed = {"telegram", "discord", "ntfy", "gotify"}
        bad = [c for c in v if c not in allowed]
        if bad:
            raise ValueError(
                f"unknown digest channels: {bad}. "
                f"Allowed: {sorted(allowed)} (Notion is excluded — per-row only)."
            )
        return v


class ParityGateConfig(BaseModel):
    """Skip push notifications while a parity check / resync is running.

    Targets Unraid + plain mdraid hosts. The probe is best-effort: missing
    `/proc/mdstat` and `mdcmd` simply means the gate is permissive (every
    notification goes through). Disabled by default — the gate is most
    useful when explicit, so the user has to opt in.

    Behaviour:
      - While the gate is active, push outputs are skipped per scan.
      - When the gate clears (next scan finds parity idle after a previous
        scan found it running), the engine flushes any analyzed items
        that weren't notified during the window — automatic catch-up.
      - The weekly digest acts as a secondary backstop and by default
        keeps firing even during parity (it's a rollup, not per-update
        noise). Flip `skip_digest_too = true` to suppress that too.
      - `homelabsage notify-pending --hours N` exposes a manual replay
        for cases where the auto-flush missed something.
    """

    enabled: bool = False
    mdstat_path: str = "/proc/mdstat"
    skip_digest_too: bool = Field(
        False,
        description=(
            "Also suppress the weekly digest while parity is running. By "
            "default the digest fires regardless — it's the backstop for "
            "real-time pings that were skipped during the parity window."
        ),
    )
