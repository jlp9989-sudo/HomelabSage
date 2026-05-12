"""Universal scheduled-job discovery.

Enumerates every "thing the OS will run for you" we can find without root
privileges or daemon-poking. Used by the curator to write per-script notes
(future work) and by the analyzer prompt to cross-reference cron entries
that exec `docker exec <container>` with the container's own note.

Sources, in increasing specificity:

  * `/etc/crontab`            — system-wide crontab, traditional shape
  * `/etc/cron.d/*`           — drop-in files, same shape as above
  * `/etc/cron.{hourly,daily,weekly,monthly}/*`
                              — periodic dirs; the schedule is implicit
                                in the dir name, the file is the script body
  * `/var/spool/cron/...`     — per-user crontabs (location varies by distro:
                                `crontabs/<user>` on Debian/Ubuntu, `<user>`
                                bare on RHEL/Arch). We probe both layouts.
  * `systemctl list-timers`   — systemd timers, when `systemctl` is on PATH
  * Unraid User Scripts       — `/boot/config/plugins/user.scripts/scripts/`,
                                each subdir holds `script` + `name` +
                                `description`. Disabled by default; enable
                                with `enable_unraid=True` (avoid mistaking
                                a non-Unraid system's `/boot` mount).

Every probe takes its root path as an argument so tests substitute a
tmpdir. Probes return [] (never raise) when their target is missing or
unreadable — running HomelabSage on a system without `/etc/cron.d` should
not crash the scan.

Out of scope for now:

  * macOS launchd (`/Library/LaunchAgents`, `~/Library/LaunchAgents`,
    `/Library/LaunchDaemons`). plist parsing is its own engineering chunk
    and "homelabbers running this on a Mac as a server" is a small
    audience. Tracked as future work in the module docstring.
  * Windows Task Scheduler. Same reason.
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ScriptTarget:
    """A scheduled or scriptable artifact discovered on the host."""

    name: str           # stable identifier per source (`source:something-unique`)
    source: str         # probe id — `etc-crontab`, `cron-d`, `cron-periodic`,
                        # `user-crontab`, `systemd-timer`, `unraid-user-scripts`
    schedule: str       # human form: cron expression, "@hourly", "every 6h", "manual"
    command: str        # the actual line that runs (already trimmed)
    path: str = ""      # filesystem origin, when applicable
    user: str = ""      # owning user when known
    body: str = ""      # full script body if separate from `command` (Unraid case)
    purpose_hint: str = ""  # human description from upstream metadata
    extra: dict[str, str] = field(default_factory=dict)


# ─── /etc/crontab + /etc/cron.d/* (shared format) ─────────────────────────

# System crontab line: `min hour dom mon dow user command…`
# (the user field is what distinguishes system-style from per-user crontabs).
_SYSTEM_CRONTAB_RE = re.compile(
    r"^\s*(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+?)\s*$"
)
# Per-user crontab line: `min hour dom mon dow command…` (no user field).
_USER_CRONTAB_RE = re.compile(
    r"^\s*(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+?)\s*$"
)
# Vixie / Fedora style: `@reboot`, `@hourly`, etc.
_SPECIAL_TIMES = {"@reboot", "@yearly", "@annually", "@monthly", "@weekly", "@daily",
                  "@midnight", "@hourly"}


def _is_comment_or_blank(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


def _parse_system_crontab(text: str, *, origin: Path) -> Iterable[ScriptTarget]:
    """Parse a `/etc/crontab`-shaped file. Skips comment / env-assignment lines."""
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if _is_comment_or_blank(raw):
            continue
        if "=" in raw and not raw.lstrip().startswith(tuple("0123456789@*")):
            # Probably `PATH=...` or `SHELL=...` env line. Skip.
            continue
        # Vixie special-time entries
        for special in _SPECIAL_TIMES:
            if raw.lstrip().startswith(special):
                rest = raw.lstrip()[len(special):].strip()
                # Format: `@daily user command` (system) or `@daily command` (user)
                parts = rest.split(maxsplit=1)
                if len(parts) == 2 and _looks_like_user(parts[0]):
                    user, cmd = parts
                else:
                    user, cmd = "", rest
                yield ScriptTarget(
                    name=f"etc-crontab:{origin.name}:{lineno}",
                    source="etc-crontab",
                    schedule=special,
                    command=cmd.strip(),
                    path=str(origin),
                    user=user,
                )
                break
        else:
            m = _SYSTEM_CRONTAB_RE.match(raw)
            if not m:
                continue
            mins, hours, dom, mon, dow, user, cmd = m.groups()
            yield ScriptTarget(
                name=f"etc-crontab:{origin.name}:{lineno}",
                source="etc-crontab",
                schedule=f"{mins} {hours} {dom} {mon} {dow}",
                command=cmd.strip(),
                path=str(origin),
                user=user,
            )


def _looks_like_user(token: str) -> bool:
    """Heuristic: token is alphanumeric/underscore/hyphen and ≤ 32 chars, no shell metachars."""
    return bool(re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,31}$", token))


def probe_etc_crontab(etc_root: Path | str = Path("/etc")) -> list[ScriptTarget]:
    """`/etc/crontab` — system crontab in the traditional 7-field shape."""
    p = Path(etc_root) / "crontab"
    if not p.is_file():
        return []
    try:
        text = p.read_text()
    except OSError:
        return []
    out = list(_parse_system_crontab(text, origin=p))
    # The probe id stays generic; the parser tagged each entry as `etc-crontab`.
    return out


def probe_etc_cron_d(etc_root: Path | str = Path("/etc")) -> list[ScriptTarget]:
    """`/etc/cron.d/*` drop-ins, same format as /etc/crontab."""
    root = Path(etc_root) / "cron.d"
    if not root.is_dir():
        return []
    out: list[ScriptTarget] = []
    for entry in sorted(root.iterdir()):
        # Debian convention: filenames must contain only [A-Za-z0-9_-]; others
        # are silently ignored by cron itself. Mirror that to avoid false hits.
        if not entry.is_file() or not re.match(r"^[A-Za-z0-9_.-]+$", entry.name):
            continue
        try:
            text = entry.read_text()
        except OSError:
            continue
        for item in _parse_system_crontab(text, origin=entry):
            # Re-tag the source so the consumer can distinguish.
            item.source = "cron-d"
            item.name = item.name.replace("etc-crontab:", "cron-d:", 1)
            out.append(item)
    return out


# ─── /etc/cron.{hourly,daily,weekly,monthly} ──────────────────────────────

_PERIODIC_DIRS = ("hourly", "daily", "weekly", "monthly")


def probe_etc_cron_periodic(etc_root: Path | str = Path("/etc")) -> list[ScriptTarget]:
    """`/etc/cron.{hourly,daily,weekly,monthly}/*` — each file IS the script.

    The schedule comes from the directory name; the body is the file itself.
    Skips backup files (ending in `.bak`, `.dpkg-*`, `~`) which `run-parts`
    also ignores.
    """
    out: list[ScriptTarget] = []
    base = Path(etc_root)
    for sub in _PERIODIC_DIRS:
        d = base / f"cron.{sub}"
        if not d.is_dir():
            continue
        for entry in sorted(d.iterdir()):
            if not entry.is_file():
                continue
            if entry.name.endswith((".bak", "~")) or ".dpkg-" in entry.name:
                continue
            try:
                body = entry.read_text(errors="replace")
            except OSError:
                continue
            out.append(ScriptTarget(
                name=f"cron-periodic:{sub}:{entry.name}",
                source="cron-periodic",
                schedule=f"@{sub}",
                command="",                # the body IS the command
                path=str(entry),
                body=body,
            ))
    return out


# ─── /var/spool/cron user crontabs ────────────────────────────────────────

def _read_user_crontab(path: Path, user: str) -> list[ScriptTarget]:
    try:
        text = path.read_text()
    except OSError:
        return []
    out: list[ScriptTarget] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if _is_comment_or_blank(raw):
            continue
        if "=" in raw and not raw.lstrip().startswith(tuple("0123456789@*")):
            continue
        for special in _SPECIAL_TIMES:
            if raw.lstrip().startswith(special):
                rest = raw.lstrip()[len(special):].strip()
                out.append(ScriptTarget(
                    name=f"user-crontab:{user}:{lineno}",
                    source="user-crontab",
                    schedule=special,
                    command=rest,
                    path=str(path),
                    user=user,
                ))
                break
        else:
            m = _USER_CRONTAB_RE.match(raw)
            if not m:
                continue
            mins, hours, dom, mon, dow, cmd = m.groups()
            out.append(ScriptTarget(
                name=f"user-crontab:{user}:{lineno}",
                source="user-crontab",
                schedule=f"{mins} {hours} {dom} {mon} {dow}",
                command=cmd.strip(),
                path=str(path),
                user=user,
            ))
    return out


def probe_user_crontabs(spool_root: Path | str = Path("/var/spool/cron")) -> list[ScriptTarget]:
    """Per-user crontabs. Two layouts coexist across distros:

      * Debian / Ubuntu: `/var/spool/cron/crontabs/<user>`
      * RHEL / Fedora / Arch: `/var/spool/cron/<user>`

    Probe both; whichever exists wins. Unreadable files (typical: not
    running as root) are silently skipped.
    """
    root = Path(spool_root)
    if not root.is_dir():
        return []
    out: list[ScriptTarget] = []
    # Debian shape first
    crontabs_dir = root / "crontabs"
    if crontabs_dir.is_dir():
        for entry in sorted(crontabs_dir.iterdir()):
            if entry.is_file():
                out.extend(_read_user_crontab(entry, entry.name))
        return out
    # RHEL shape
    for entry in sorted(root.iterdir()):
        if entry.is_file() and re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,31}$", entry.name):
            out.extend(_read_user_crontab(entry, entry.name))
    return out


# ─── systemd timers ───────────────────────────────────────────────────────

def probe_systemd_timers(systemctl: str = "systemctl",
                         *, runner=subprocess.run) -> list[ScriptTarget]:
    """`systemctl list-timers` output.

    Returns empty list when `systemctl` is missing (containers, BSDs, macOS)
    or when it fails for any reason. The probe is purely additive — failure
    must never block other sources.

    `runner` is injectable so tests don't shell out.
    """
    if not shutil.which(systemctl):
        return []
    try:
        proc = runner(
            [systemctl, "list-timers", "--all", "--no-legend", "--no-pager"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out: list[ScriptTarget] = []
    # Format varies between systemd versions but the last two fixed columns are
    # always UNIT and ACTIVATES. The leading columns are timestamps we don't need.
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        # Heuristic: a unit name ends in `.timer`; the thing it activates ends
        # in `.service`. Pick those two out of the line regardless of position.
        timer = next((p for p in parts if p.endswith(".timer")), "")
        service = next((p for p in parts if p.endswith(".service")), "")
        if not timer:
            continue
        out.append(ScriptTarget(
            name=f"systemd-timer:{timer}",
            source="systemd-timer",
            schedule="(see `systemctl show " + timer + "`)",
            command=service,
            path="",
            extra={"activates": service},
        ))
    return out


# ─── Unraid User Scripts ──────────────────────────────────────────────────

def probe_unraid_user_scripts(
    scripts_root: Path | str = Path("/boot/config/plugins/user.scripts/scripts"),
) -> list[ScriptTarget]:
    """Each subdir of `scripts_root` is one script. Files: `script`, `name`,
    `description`. The schedule lives in a separate JSON config that we
    don't currently parse (CLAUDE.md memory:
    `feedback_user_scripts_schedule_json`); marked `manual` until that work
    lands.

    Disabled in the default probe set — having `/boot` on a non-Unraid
    system means something else (UEFI partition, raspberry pi rootfs).
    Caller must opt in.
    """
    root = Path(scripts_root)
    if not root.is_dir():
        return []
    out: list[ScriptTarget] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        script_file = sub / "script"
        if not script_file.is_file():
            continue
        try:
            body = script_file.read_text(errors="replace")
        except OSError:
            continue
        name = sub.name
        with contextlib.suppress(OSError):
            name = (sub / "name").read_text().strip() or name
        purpose = ""
        with contextlib.suppress(OSError):
            purpose = (sub / "description").read_text().strip()
        out.append(ScriptTarget(
            name=f"unraid-user-scripts:{sub.name}",
            source="unraid-user-scripts",
            schedule="manual",
            command="",
            path=str(script_file),
            body=body,
            purpose_hint=purpose,
            extra={"display_name": name},
        ))
    return out


# ─── orchestrator ─────────────────────────────────────────────────────────

@dataclass
class DiscoveryRoots:
    """Override every probe's filesystem root in one shot — used by tests
    and by config (e.g. when `/etc` lives somewhere unusual)."""

    etc: Path = Path("/etc")
    cron_spool: Path = Path("/var/spool/cron")
    unraid_user_scripts: Path = Path("/boot/config/plugins/user.scripts/scripts")
    systemctl: str = "systemctl"


def discover_scripts(
    roots: DiscoveryRoots | None = None,
    *,
    enable_systemd: bool = True,
    enable_unraid: bool = False,
) -> list[ScriptTarget]:
    """Run every enabled probe; concat results. The order in the output is
    deterministic across runs so consumers can compare scans."""
    r = roots or DiscoveryRoots()
    out: list[ScriptTarget] = []
    out.extend(probe_etc_crontab(r.etc))
    out.extend(probe_etc_cron_d(r.etc))
    out.extend(probe_etc_cron_periodic(r.etc))
    out.extend(probe_user_crontabs(r.cron_spool))
    if enable_systemd:
        out.extend(probe_systemd_timers(r.systemctl))
    if enable_unraid:
        out.extend(probe_unraid_user_scripts(r.unraid_user_scripts))
    return out
