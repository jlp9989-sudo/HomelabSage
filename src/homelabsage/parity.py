"""Parity-aware notification gate (Unraid + raw mdraid).

Why: a parity check / resync can take 12-30 hours on a 20-TB array, and an
out-of-band notification mid-window is hostile noise — the user is already
watching the array health. We skip *push* notifications during that window
(Telegram / Discord / Ntfy / Gotify) so the user's phone stays quiet; the
DB (and Notion if enabled) still records everything so nothing is lost.

Detection:
  - Unraid: `mdcmd status` returns key=value lines including `mdResync=N`
    (N>0 means a resync/parity check is running) and `mdState` (the array
    state). Both files are exposed inside any container that mounts
    `/var/lib/docker.sock` and runs on the host's PID/network namespace —
    but `mdcmd` is host-only and not available inside a stock container, so
    the more portable signal is `/proc/mdstat`.
  - Plain mdraid (Debian, Fedora, …): `/proc/mdstat` shows a `[====>....]`
    progress bar plus a `recovery`/`check`/`resync` keyword on the line
    following the array definition.

Both detectors fail safe — returning "not running" when their source is
absent — so a non-RAID host never gates notifications.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


_MDSTAT_TRIGGERS = ("resync", "recovery", "check", "reshape")


@dataclass
class ParityState:
    """Result of a single gate probe."""

    running: bool
    reason: str  # human-readable, used for log lines / digest mentions

    def __bool__(self) -> bool:
        return self.running


def _read_mdstat(path: str | Path = "/proc/mdstat") -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.debug("parity: cannot read %s: %s", path, e)
        return None


def check_mdstat(path: str | Path = "/proc/mdstat") -> ParityState:
    """Scan `/proc/mdstat` for an in-progress resync / check / reshape.

    The trigger is a line that contains one of the rebuild keywords
    plus a percentage. mdraid emits these on its own line right under the
    array definition while the operation is running; once it finishes the
    line vanishes, so the simple substring match is both sufficient and
    cheap.
    """
    text = _read_mdstat(path)
    if text is None:
        return ParityState(running=False, reason="/proc/mdstat absent")
    for line in text.splitlines():
        low = line.lower().strip()
        if any(tok in low for tok in _MDSTAT_TRIGGERS):
            # Pluck out percentage for the log line / digest mention.
            return ParityState(
                running=True,
                reason=f"mdraid: {line.strip()}",
            )
    return ParityState(running=False, reason="mdstat clean")


def check_mdcmd() -> ParityState | None:
    """Parse `mdcmd status` (Unraid host). None when the binary is absent.

    The relevant lines are:
      mdState=STARTED        (array up; could still be resyncing)
      mdResync=12345         (>0 → operation in progress)
      mdResyncCorr=0         (corrective resync after disk failure)
    Anything else is fine.
    """
    if not shutil.which("mdcmd"):
        return None
    try:
        result = subprocess.run(
            ["mdcmd", "status"],
            timeout=5.0, check=False,
            capture_output=True, text=True, errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.debug("mdcmd status failed: %s", e)
        return None
    if result.returncode != 0:
        return None
    state: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            state[k.strip()] = v.strip()
    resync = state.get("mdResync", "0")
    try:
        resync_n = int(resync)
    except ValueError:
        resync_n = 0
    if resync_n > 0:
        size = state.get("mdResyncSize", "?")
        return ParityState(
            running=True,
            reason=f"Unraid parity/resync in progress (mdResync={resync} / size={size})",
        )
    return ParityState(running=False, reason=f"mdcmd state={state.get('mdState', '?')}")


def is_parity_running(*, mdstat_path: str | Path = "/proc/mdstat") -> ParityState:
    """Single combined gate — prefers mdcmd when available, falls back to mdstat.

    Returns ParityState(running=True/False, reason=...). Reason is always
    populated so the engine can log *why* a notification was gated even on
    a happy host.
    """
    via_mdcmd = check_mdcmd()
    if via_mdcmd is not None and via_mdcmd.running:
        return via_mdcmd
    via_mdstat = check_mdstat(mdstat_path)
    if via_mdstat.running:
        return via_mdstat
    # Combine the reasons so callers can see what was checked.
    parts = [via_mdcmd.reason if via_mdcmd else "no mdcmd", via_mdstat.reason]
    return ParityState(running=False, reason=" | ".join(parts))
