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


class BackupRepoConfig(BaseModel):
    """One backup repository the staleness adapter should probe.

    Credentials never live in homelabsage's config — instead the user
    pre-exports them in the environment (the same way their nightly cron
    does), or names them here under `env` so the subprocess inherits them.
    Examples:

      env:
        RESTIC_REPOSITORY: "s3:s3.example.com/my-repo"
        RESTIC_PASSWORD_FILE: "/secrets/restic-password"

      env:
        BORG_REPO: "/mnt/backups/borg"
        BORG_PASSPHRASE: "${BORG_PASSPHRASE_ENV}"
    """

    name: str = Field(..., description="Short label used in audit reports.")
    tool: str = Field(
        "restic",
        description="One of `restic`, `borg`, `kopia`.",
    )
    env: dict[str, str] = Field(default_factory=dict)
    binary: str = Field(
        "",
        description="Override path to the backup binary. Empty = use PATH.",
    )
    warn_after_days: int = Field(
        2,
        description="Staleness threshold for a `medium` audit finding.",
    )
    critical_after_days: int = Field(
        7,
        description="Staleness threshold for a `critical` audit finding.",
    )
    timeout_seconds: int = Field(
        60,
        description="Hard cap on the snapshot-listing subprocess.",
    )

    @field_validator("tool")
    @classmethod
    def _check_tool(cls, v: str) -> str:
        allowed = {"restic", "borg", "kopia"}
        if v.lower() not in allowed:
            raise ValueError(f"tool must be one of {sorted(allowed)}, got {v!r}")
        return v.lower()


class BackupHealthConfig(BaseModel):
    """Probe a list of restic/borg/kopia repos and emit auditor findings.

    Each repo is probed once per scan. Failure is best-effort: a missing
    binary, an unreachable repo or a wrong password becomes a `high`
    audit finding rather than crashing the scan.
    """

    enabled: bool = False
    repos: list[BackupRepoConfig] = Field(default_factory=list)
    # When true, append the most recent BackupHealthReport.to_context() to
    # `Update.context.backup_health` for every update emitted in the same
    # scan. Lets the analyzer say "you're on stale backups — hold this
    # upgrade until they recover" without an extra fetch per row.
    inject_into_updates: bool = False


class HealthCheckConfig(BaseModel):
    """Post-update health check — verifies containers stay healthy after applying.

    The probe fires N minutes after `status` flips to APPLIED, scans
    `docker logs --since=<applied_at>` for a small fixed list of known
    bad signals (silent CPU fallback from CUDA/ROCm, OOM exits, panic
    backtraces) and writes one row to `health_checks` per probe. The
    auditor + analyzer pick those rows up the next time they run.
    """

    enabled: bool = False
    grace_minutes: int = Field(
        10,
        description=(
            "How long to wait after status=APPLIED before probing. Most "
            "containers settle in 1-3 minutes; the default keeps the probe "
            "well outside the cold-start window."
        ),
    )
    log_tail_lines: int = Field(
        2000,
        description="How many recent lines to inspect per container.",
    )
    extra_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Extra regex patterns flagged as bad signals on top of the "
            "built-in set (`cuda fallback`, `rocm fallback`, OOM kills, "
            "`panic:` backtraces)."
        ),
    )


class LogAnomalyConfig(BaseModel):
    """Proactive per-container log scanner.

    Reads each running container's recent docker logs, tokenises the
    ERROR/WARN density, and flags any container whose error rate exceeds
    `sigma_threshold` standard deviations above its own rolling baseline.
    Disabled by default — the per-container baseline takes a few scans
    to stabilise.
    """

    enabled: bool = False
    lookback_minutes: int = Field(
        60,
        description="How far back to look in each container's logs.",
    )
    sigma_threshold: float = Field(
        3.0,
        description=(
            "How many standard deviations above the rolling baseline counts "
            "as an anomaly. Lower → noisier, higher → only the loudest "
            "outliers."
        ),
    )
    min_samples: int = Field(
        7,
        description=(
            "Don't fire alerts until the baseline has at least this many "
            "samples — protects against the first few scans being "
            "interpreted as deviations."
        ),
    )
    container_skip: list[str] = Field(
        default_factory=list,
        description="Container name regexes to skip (e.g. `tailscale|caddy`).",
    )


class I18nConfig(BaseModel):
    """UI language selector. Minimal — only nav + dashboard headings are
    translated for now; per-page deep i18n stays a non-goal until usage
    justifies it."""

    lang: str = Field(
        "en",
        description="UI language. Supported: `en` (English), `es` (Spanish).",
    )

    @field_validator("lang")
    @classmethod
    def _check_lang(cls, v: str) -> str:
        if v.lower() not in {"en", "es"}:
            raise ValueError(f"lang must be 'en' or 'es', got {v!r}")
        return v.lower()
