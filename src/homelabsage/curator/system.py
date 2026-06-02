"""System-level curator note (`notes/system.md`).

Captures host-environment facts so every subsequent LLM call (analyzer and
per-container curator) has shared baseline context without paying the token
cost of rediscovering it for each update. The probes below are best-effort
— each returns `None` when its target binary or file is absent so the same
codebase runs cleanly on Debian, Fedora, Alpine, Unraid, macOS dev
laptops, etc.

Architectural choice: deterministic Markdown by default, LLM polish opt-in.
The host facts are themselves structured (uname output, JSON from
`docker info`, `lspci` lines) so an LLM pass adds little signal at temperature
0 and risks paraphrasing values into something subtly different. The user can
flip on the LLM polish via `--polish` for narrative wording.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Cap probe runtime — none of these should take more than a second on a
# healthy system. We still set a hard timeout so a stuck `zpool list` cannot
# hang the curate.
_PROBE_TIMEOUT = 5.0


def _run(cmd: list[str], *, timeout: float = _PROBE_TIMEOUT) -> str | None:
    """Run a shell command, return stdout text or None on any failure.

    Failure modes covered: binary missing (FileNotFoundError), non-zero
    exit, timeout, decode error. We log at DEBUG level so probes that
    expectedly fail (e.g. `zpool list` on a non-ZFS host) don't pollute
    INFO output.
    """
    if not cmd or not shutil.which(cmd[0]):
        return None
    try:
        result = subprocess.run(
            cmd,
            timeout=timeout,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.debug("probe %s failed: %s", cmd[0], e)
        return None
    if result.returncode != 0:
        log.debug(
            "probe %s exited non-zero: rc=%s stderr=%s",
            cmd[0], result.returncode, result.stderr[:200]
        )
        return None
    return result.stdout.strip()


def _read_file(path: str | Path) -> str | None:
    """Read a small text file or return None. Handles `/proc/*` quirks."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as e:
        log.debug("probe read %s failed: %s", path, e)
        return None


# ─── Individual probes ──────────────────────────────────────────────────


def probe_kernel() -> dict[str, str] | None:
    """`uname -r` + `/etc/os-release` snippet (PRETTY_NAME, ID, VERSION_ID).

    Returns a small dict suitable for direct rendering. None when neither
    signal is available (unlikely outside very stripped-down containers).
    """
    out: dict[str, str] = {}
    kernel = _run(["uname", "-r"])
    if kernel:
        out["kernel"] = kernel
    osrel = _read_file("/etc/os-release")
    if osrel:
        for line in osrel.splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if k in {"PRETTY_NAME", "ID", "VERSION_ID"}:
                out[k.lower()] = v
    return out or None


def probe_docker_info() -> dict[str, Any] | None:
    """`docker info --format '{{json .}}'` — pull a handful of stable fields.

    We deliberately don't return the full JSON (it's large and changes
    frequently). The four fields below are what the analyzer actually
    benefits from when reasoning about updates: storage driver decides
    whether a layer rewrite is expensive, default runtime decides whether
    GPU containers will get nvidia/rocm, cgroup driver affects
    systemd-aware images.
    """
    raw = _run(["docker", "info", "--format", "{{json .}}"], timeout=10)
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        log.debug("docker info JSON parse failed: %s", e)
        return None
    return {
        "server_version": info.get("ServerVersion"),
        "storage_driver": info.get("Driver"),
        "cgroup_driver": info.get("CgroupDriver"),
        "default_runtime": info.get("DefaultRuntime"),
        "runtimes": sorted((info.get("Runtimes") or {}).keys()),
        "operating_system": info.get("OperatingSystem"),
        "kernel_version": info.get("KernelVersion"),
        "ncpu": info.get("NCPU"),
        "mem_total_bytes": info.get("MemTotal"),
    }


def probe_cpu() -> dict[str, str] | None:
    """First "model name" line from `/proc/cpuinfo` (Linux only)."""
    text = _read_file("/proc/cpuinfo")
    if not text:
        return None
    for line in text.splitlines():
        if line.lower().startswith("model name"):
            _, _, val = line.partition(":")
            return {"model": val.strip()}
    return None


def probe_memory() -> dict[str, int] | None:
    """MemTotal from `/proc/meminfo`, in kilobytes (Linux only)."""
    text = _read_file("/proc/meminfo")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            try:
                return {"total_kb": int(parts[1])}
            except (IndexError, ValueError):
                return None
    return None


def probe_gpus() -> list[dict[str, str]] | None:
    """Detect attached GPUs via nvidia-smi or rocm-smi.

    Single-row format for nvidia-smi (`--query-gpu`) and a permissive parse
    for rocm-smi text. Returns a list because dual-vendor hosts are
    increasingly common (consumer NVIDIA + AMD APU for compute).
    """
    gpus: list[dict[str, str]] = []
    nv = _run(
        ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
         "--format=csv,noheader,nounits"]
    )
    if nv:
        for line in nv.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                gpus.append({
                    "vendor": "nvidia",
                    "name": parts[0],
                    "driver": parts[1],
                    "memory_mib": parts[2],
                })
    rocm = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"])
    if rocm:
        try:
            data = json.loads(rocm)
            for k, v in data.items():
                if not isinstance(v, dict):
                    continue
                gpus.append({
                    "vendor": "amd",
                    "name": v.get("Card Series") or v.get("Card model") or k,
                    "driver": v.get("Driver version", ""),
                    "memory_mib": str(v.get("VRAM Total Memory (B)", "")),
                })
        except json.JSONDecodeError:
            log.debug("rocm-smi returned non-JSON")
    return gpus or None


def probe_zpools() -> list[dict[str, str]] | None:
    """`zpool list -H` returns one tab-separated row per pool. Empty list →
    ZFS installed but no pools; None → ZFS not installed."""
    out = _run(["zpool", "list", "-H", "-o", "name,size,health,frag,cap"])
    if out is None:
        return None
    pools: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            pools.append({
                "name": parts[0],
                "size": parts[1],
                "health": parts[2],
                "fragmentation": parts[3],
                "capacity": parts[4],
            })
    return pools or None


def probe_unraid() -> dict[str, Any] | None:
    """Detect Unraid + summary of array status if available.

    Unraid keeps user-config under `/boot/config` (Slackware on a FAT
    USB). The presence of that directory is a reliable detector. `mdcmd
    status` provides array health; we parse a few representative lines.
    """
    boot_cfg = Path("/boot/config")
    if not boot_cfg.is_dir():
        return None
    info: dict[str, Any] = {"is_unraid": True}
    version = _read_file("/etc/unraid-version")
    if version:
        # File looks like `version="7.2.0"`
        for token in version.split():
            if token.startswith("version="):
                info["version"] = token.split("=", 1)[1].strip('"')
    # mdcmd is Slackware-only; on the container side it won't exist.
    md = _run(["mdcmd", "status"])
    if md:
        flags: dict[str, str] = {}
        for line in md.splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k in {"mdState", "mdNumDisks", "mdNumInvalid", "mdNumDisabled",
                     "mdResync", "mdResyncCorr", "mdResyncSize", "mdResyncPos"}:
                flags[k] = v.strip()
        if flags:
            info["array"] = flags
    return info


def probe_network_proxy() -> dict[str, str] | None:
    """Detect a reverse-proxy / tunnel binary on the host PATH.

    We only check for the *presence* of the binary, not its config — the
    point is to give the LLM a hint that "this host runs Caddy / Traefik /
    cloudflared / nginx / tailscale" so suggestions like "expose port X"
    can be qualified with "and add a vhost in your proxy".
    """
    found: dict[str, str] = {}
    for name in ("caddy", "traefik", "nginx", "haproxy", "cloudflared", "tailscale"):
        path = shutil.which(name)
        if path:
            found[name] = path
    return found or None


# ─── Aggregation + render ───────────────────────────────────────────────


@dataclass
class SystemReport:
    """Frozen view of every probe that returned data."""

    kernel: dict[str, str] | None = None
    docker: dict[str, Any] | None = None
    cpu: dict[str, str] | None = None
    memory: dict[str, int] | None = None
    gpus: list[dict[str, str]] | None = None
    zpools: list[dict[str, str]] | None = None
    unraid: dict[str, Any] | None = None
    network: dict[str, str] | None = None
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def fingerprint(self) -> str:
        """Stable hex digest of structural facts.

        Used in the curator footer so re-runs on unchanged hosts produce
        byte-identical files (the footer carries the fingerprint;
        `generated_at` is excluded so the timestamp doesn't churn the
        digest). 12 hex chars is enough — collisions on the same host are
        not a concern, this isn't a security boundary.
        """
        payload = {
            "kernel": self.kernel,
            "docker": (self.docker or {}).get("server_version") if self.docker else None,
            "cpu": self.cpu,
            "memory": self.memory,
            "gpus": self.gpus,
            "zpools": [(p.get("name"), p.get("size")) for p in (self.zpools or [])],
            "unraid": (self.unraid or {}).get("version") if self.unraid else None,
            "network": sorted((self.network or {}).keys()),
        }
        return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def collect() -> SystemReport:
    """Run every probe and assemble a SystemReport. Never raises."""
    return SystemReport(
        kernel=probe_kernel(),
        docker=probe_docker_info(),
        cpu=probe_cpu(),
        memory=probe_memory(),
        gpus=probe_gpus(),
        zpools=probe_zpools(),
        unraid=probe_unraid(),
        network=probe_network_proxy(),
    )


def render(report: SystemReport) -> str:
    """Render a `SystemReport` as Markdown with a stable curator footer.

    The footer format mirrors the per-container curator:
      `<!-- curator: system@<fingerprint> -->`
    so future runs can detect "this file is up to date, skip write" by
    comparing fingerprints.
    """
    lines: list[str] = ["# System", ""]

    if report.kernel:
        lines.append("## OS / kernel")
        if "pretty_name" in report.kernel:
            lines.append(f"- {report.kernel['pretty_name']}")
        if "kernel" in report.kernel:
            lines.append(f"- kernel: `{report.kernel['kernel']}`")
        if "id" in report.kernel and "version_id" in report.kernel:
            lines.append(f"- id: {report.kernel['id']} {report.kernel['version_id']}")
        lines.append("")

    if report.cpu or report.memory:
        lines.append("## Resources")
        if report.cpu and "model" in report.cpu:
            lines.append(f"- CPU: {report.cpu['model']}")
        if report.memory and "total_kb" in report.memory:
            gib = report.memory["total_kb"] / 1024 / 1024
            lines.append(f"- RAM: {gib:.1f} GiB")
        if report.gpus:
            for gpu in report.gpus:
                line = f"- GPU ({gpu['vendor']}): {gpu['name']}"
                if gpu.get("memory_mib"):
                    line += f" — {gpu['memory_mib']} MiB"
                if gpu.get("driver"):
                    line += f" (driver {gpu['driver']})"
                lines.append(line)
        lines.append("")

    if report.docker:
        d = report.docker
        lines.append("## Docker daemon")
        if d.get("server_version"):
            lines.append(f"- version: {d['server_version']}")
        if d.get("storage_driver"):
            lines.append(f"- storage driver: {d['storage_driver']}")
        if d.get("cgroup_driver"):
            lines.append(f"- cgroup driver: {d['cgroup_driver']}")
        if d.get("default_runtime"):
            lines.append(f"- default runtime: {d['default_runtime']}")
        if d.get("runtimes"):
            lines.append(f"- runtimes: {', '.join(d['runtimes'])}")
        lines.append("")

    if report.zpools:
        lines.append("## ZFS pools")
        for p in report.zpools:
            lines.append(
                f"- `{p['name']}` — {p['size']} / health {p['health']} "
                f"/ frag {p['fragmentation']} / cap {p['capacity']}"
            )
        lines.append("")

    if report.unraid:
        lines.append("## Unraid")
        if "version" in report.unraid:
            lines.append(f"- version: {report.unraid['version']}")
        array = report.unraid.get("array") or {}
        for k, v in array.items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    if report.network:
        lines.append("## Network / proxy binaries on PATH")
        for name, path in sorted(report.network.items()):
            lines.append(f"- {name} → `{path}`")
        lines.append("")

    if len(lines) == 2:
        # Only the header — every probe came back empty (likely running
        # inside a stripped container with nothing mounted from the host).
        lines.append(
            "_No probes returned data. Run the curator from the host or "
            "mount `/etc`, `/proc`, `/var/run/docker.sock` into the "
            "container so probes can see the host environment._"
        )
        lines.append("")

    lines.append(
        f"<!-- curator: system@{report.fingerprint()} "
        f"generated_at={report.generated_at.isoformat()} -->"
    )
    return "\n".join(lines)


def write_to_notes(notes_dir: str | Path, body: str) -> Path | None:
    """Atomic write of `system.md` into the curator's notes dir.

    Returns the written path, or None when `notes_dir` is empty/missing.
    Idempotent: when the existing file's fingerprint matches `body`'s, the
    write is skipped so noisy file-watchers don't re-fire.
    """
    if not notes_dir:
        return None
    out_dir = Path(notes_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("system.md mkdir failed: %s", e)
        return None
    target = out_dir / "system.md"

    new_fp = _footer_fingerprint(body)
    if target.exists():
        cur_fp = _footer_fingerprint(target.read_text(encoding="utf-8", errors="replace"))
        if cur_fp and cur_fp == new_fp:
            log.debug("system.md unchanged (fingerprint=%s); skip write", new_fp)
            return target

    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)
    return target


def _footer_fingerprint(text: str) -> str | None:
    """Pull the `system@<fp>` token from the curator footer, if present."""
    for line in reversed(text.splitlines()):
        if "system@" in line and "<!-- curator:" in line:
            try:
                fp = line.split("system@", 1)[1].split()[0]
                return fp
            except (IndexError, ValueError):
                return None
    return None


def curate_system(notes_dir: str | Path) -> tuple[Path | None, SystemReport]:
    """One-shot: probe → render → write. Returns `(path, report)`."""
    report = collect()
    body = render(report)
    path = write_to_notes(notes_dir, body)
    return path, report
