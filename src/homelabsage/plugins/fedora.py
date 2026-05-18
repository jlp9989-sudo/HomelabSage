"""Fedora plugin — surface dnf updates from a remote host via SSH.

Why SSH and not a local agent: most homelabs run Fedora on bare metal or
in a VM, separate from wherever HomelabSage itself lives. Pushing a
sidecar agent for one host is overkill; an SSH read is one round trip
and works for any number of hosts behind the scheduler.

The plugin is opt-in (`sources.fedora.enabled = false` by default) and
needs three things in config: `host`, `user`, and a key path that's
mounted into the container. No sudo required — `dnf check-update`
reads metadata as any user.

Stability bias (matches the user's "estabilidad > bleeding edge"
preference): we split the typically-large dnf output into:

  - Individual `Update` items for packages matched by `critical_packages`
    (kernel, linux-firmware, mesa, rocm, vulkan, llvm, glibc, systemd,
    docker/podman, openssh, dnf, selinux-policy). Each one becomes an
    LLM-analyzed entry in the dashboard.
  - One aggregated `fedora-userspace` summary `Update` covering the rest
    so the user sees the total count without an LLM call per package.

That keeps the analyzer cheap (≤ 5-6 critical packages on a stable
release vs 30-150 total) while still flagging the upgrades that
historically broke this user's setup (kernel/firmware regressions).
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from ..config import FedoraSourceConfig
from ..models import Update
from . import Plugin

log = logging.getLogger(__name__)


# Lines in `dnf check-update` output we treat as data. A real package
# line is `<name>.<arch>  <version>  <repo>`. Anything that does not
# parse this way (banner, "Obsoleting Packages" section, empty, etc.) is
# skipped — the parser is forgiving on purpose, dnf decorations change
# between releases and we don't want to break on cosmetics.
_PKG_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9._+-]+)\.(?P<arch>[a-z0-9_]+)\s+(?P<version>\S+)\s+(?P<repo>\S+)\s*$"
)


def parse_dnf_check_update(stdout: str) -> list[dict[str, str]]:
    """Turn the raw `dnf check-update` output into structured rows.

    Returns a list of dicts with keys `name`, `arch`, `version`, `repo`.
    Lines that don't match the standard 3-column shape are ignored.

    Stops parsing at the literal "Obsoleting Packages" header — anything
    below it is a different section dnf adds when there are obsoletions,
    and the line shape there is intentionally different.
    """
    rows: list[dict[str, str]] = []
    in_obsoleting = False
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lower().startswith("obsoleting packages"):
            in_obsoleting = True
            continue
        if in_obsoleting:
            continue
        m = _PKG_LINE_RE.match(line)
        if not m:
            continue
        rows.append(
            {
                "name": m.group("name"),
                "arch": m.group("arch"),
                "version": m.group("version"),
                "repo": m.group("repo"),
            }
        )
    return rows


def split_critical(
    rows: list[dict[str, str]], critical_patterns: list[str]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Partition rows into (critical, others) by name-regex match."""
    if not critical_patterns:
        return [], rows
    compiled = [re.compile(p) for p in critical_patterns]
    crit: list[dict[str, str]] = []
    other: list[dict[str, str]] = []
    for row in rows:
        if any(p.search(row["name"]) for p in compiled):
            crit.append(row)
        else:
            other.append(row)
    return crit, other


def build_userspace_summary(others: list[dict[str, str]], *, max_listed: int = 25) -> str:
    """Build the body shown to the LLM for the aggregated entry."""
    if not others:
        return ""
    head = others[:max_listed]
    lines = [f"- {r['name']}.{r['arch']} → {r['version']} (from {r['repo']})" for r in head]
    body = "\n".join(lines)
    if len(others) > max_listed:
        body += f"\n…and {len(others) - max_listed} more."
    return body


class FedoraPlugin(Plugin):
    """Pulls `dnf check-update` from a remote Fedora host over SSH."""

    id = "fedora"

    def __init__(self, cfg: FedoraSourceConfig):
        self.cfg = cfg

    async def scan(self) -> list[Update]:
        if not self.cfg.enabled:
            return []
        if not (self.cfg.host and self.cfg.user and self.cfg.ssh_key_path):
            log.warning("fedora: enabled but host/user/ssh_key_path missing — skipping")
            return []
        key_path = Path(self.cfg.ssh_key_path)
        if not key_path.is_file():
            log.warning("fedora: ssh_key_path %s not readable — skipping", key_path)
            return []

        try:
            stdout = await self._run_check_update(key_path)
        except Exception as e:
            log.exception("fedora: SSH/dnf failed on %s@%s: %s", self.cfg.user, self.cfg.host, e)
            return []

        rows = parse_dnf_check_update(stdout)
        if not rows:
            log.info("fedora: no pending updates on %s", self.cfg.host)
            return []
        return self._emit_updates(rows)

    def _emit_updates(self, rows: list[dict[str, str]]) -> list[Update]:
        """Split into critical-as-individual + (optionally) one aggregate."""
        crit, others = split_critical(rows, self.cfg.critical_packages)
        out: list[Update] = []
        for row in crit:
            out.append(
                Update(
                    source=self.id,
                    subject=row["name"],
                    current_version="installed",  # dnf doesn't print current in check-update
                    new_version=row["version"],
                    context={
                        "arch": row["arch"],
                        "repo": row["repo"],
                        "host": self.cfg.host,
                    },
                )
            )
        if self.cfg.aggregate_others and others:
            summary = build_userspace_summary(others)
            out.append(
                Update(
                    source=self.id,
                    subject="fedora-userspace",
                    current_version="installed",
                    new_version=f"{len(others)} packages pending",
                    release_notes=summary,
                    context={
                        "host": self.cfg.host,
                        "package_count": len(others),
                    },
                )
            )
        return out

    async def _run_check_update(self, key_path: Path) -> str:
        """SSH + `dnf check-update` in a worker thread (paramiko is blocking).

        Why paramiko and not asyncssh: paramiko has no runtime deps beyond
        cryptography (already pulled in transitively), no event-loop hooks,
        and the call is one-shot — running it in a thread keeps the async
        engine loop free without adding asyncssh's larger surface area.
        """
        # Lazy import — paramiko is only loaded when the plugin actually runs.
        # Keeps `homelabsage --help` snappy and isolates the dep to this code path.
        import paramiko

        def _blocking() -> str:
            client = paramiko.SSHClient()
            if self.cfg.known_hosts_path:
                try:
                    client.load_host_keys(self.cfg.known_hosts_path)
                except FileNotFoundError:
                    log.warning(
                        "fedora: known_hosts %s not found, falling back to AutoAdd",
                        self.cfg.known_hosts_path,
                    )
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            else:
                # First-run convenience — log so the user knows it happened.
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                log.warning(
                    "fedora: no known_hosts configured; accepting host key for %s",
                    self.cfg.host,
                )
            try:
                pkey = paramiko.Ed25519Key.from_private_key_file(str(key_path))
            except paramiko.SSHException:
                pkey = paramiko.RSAKey.from_private_key_file(str(key_path))
            client.connect(
                hostname=self.cfg.host,
                port=self.cfg.port,
                username=self.cfg.user,
                pkey=pkey,
                timeout=self.cfg.timeout_seconds,
                allow_agent=False,
                look_for_keys=False,
            )
            try:
                # `--refresh` forces dnf to re-pull metadata so we don't
                # report stale results on a long-running host. `--quiet`
                # strips the progress banner the parser would skip anyway.
                # Exit code 100 means "updates available" — not an error.
                stdin, stdout, stderr = client.exec_command(
                    "dnf check-update --refresh --quiet",
                    timeout=self.cfg.timeout_seconds,
                )
                data = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
                rc = stdout.channel.recv_exit_status()
                if rc not in (0, 100):
                    log.warning(
                        "fedora: dnf exit=%s on %s (stderr=%s)", rc, self.cfg.host, err.strip()
                    )
                return data
            finally:
                client.close()

        return await asyncio.to_thread(_blocking)
