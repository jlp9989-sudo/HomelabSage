"""Curator — turns each running container into a written Markdown note.

The curator complements the analyzer side of HomelabSage. The analyzer consumes
the user's notes when judging upcoming updates. The curator's job is to MAKE
those notes in the first place, so the next analyzer run has something to read.

Design choices for the MVP:
  - One note per container, written to `curator.output_dir` (defaults to
    `notes.notes_dir` so the analyzer picks them up automatically).
  - The note's filename is `{container_name}.md`.
  - Each generated note ends with a deterministic footer marker like
    `<!-- curator: <container>@<image_digest_short> -->`.
  - Dedup rules on re-runs:
      * footer present + same digest    → skip (already current).
      * footer present + digest changed → regenerate (image rebuilt).
      * file exists without footer      → skip (user wrote it by hand).
      * `--force` bypasses all of the above.
  - The prompt template is configurable via `curator.prompt_template_path`.
    Built-in default is intentionally provider-agnostic — no JSON mode, no
    function calling, no model-specific phrasing. Plain instructions only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docker
from docker.models.containers import Container

from .config import CuratorConfig, DockerSourceConfig
from .github import list_releases
from .llm import LLMClient
from .plugins.docker import DockerPlugin

log = logging.getLogger(__name__)


# ─── Public surface ──────────────────────────────────────────────────────


PROMPT_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "container_name",
        "image",
        "repo",
        "current_version",
        "ports",
        "mounts",
        "env_vars",
        "labels",
        "release_notes",
        "style_examples",
    }
)


DEFAULT_PROMPT_TEMPLATE = """\
You are documenting a service running in the user's homelab. Your job is to write a short Markdown note that captures the important facts about this container so a future maintainer (or another tool) can read it in under a minute.

Rules for the note:

1. Open with ONE sentence stating the PURPOSE of this service for the user (the "why" it exists), not what its software does in general.
2. Then add 2 to 5 short bullet points. Each bullet should be a fact a future maintainer would actually need:
   - Version pins, versionlocks, or specific versions known to be broken
   - Critical environment variables, mount paths, or ports
   - Dependencies on other services in the homelab
   - Known traps, workarounds, or quirks
3. Do not restate facts that `docker inspect` would already show (image name, full port list, full env list). Only mention them if they carry a non-obvious meaning.
4. You may group bullets under `## Section Name` headers if it helps reading. Headers are optional.
5. Keep the total note under 30 lines.
6. Output ONLY the Markdown body. No code fences, no preamble, no closing remarks.
7. If you do not have enough information to write the PURPOSE sentence, write exactly this single line and stop: `(no purpose stated yet — fill in)`. Do not invent reasons.
8. NEVER invent facts. Do not assign meaning to container name suffixes, image tag variants, env var values, or settings unless the inputs above explicitly state that meaning. If the inputs do not support a bullet, omit the bullet entirely — fewer bullets are better than speculation. In particular:
   - Do not guess what a name suffix like `-pnp`, `-lts`, `-edge`, etc. means.
   - Do not speculate about whether a setting is "overridden", "unused", "ignored", or "deprecated" without evidence in the inputs.
   - Do not invent network behavior, security posture, or integration details that are not in the inputs.
9. NEVER quote specific version numbers, release dates, PR numbers, issue numbers, or commit hashes unless they appear verbatim in the inputs above (in the `# Container facts` block as `current version`, or inside the `# Recent upstream releases` block). Do not extrapolate "the next version", "the latest release", or "released on <date>" — if the input says current version is 2.19.5 and the recent releases block is empty, the only honest statement is "running 2.19.5; no upstream releases visible". Never compute a "+1 minor" or guess a future tag.

# Container facts
- name: {container_name}
- image: {image}
- repo: {repo}
- current version: {current_version}
- ports (published): {ports}
- mounts: {mounts}
- environment variables (secrets redacted): {env_vars}
- labels of interest: {labels}

# Recent upstream releases
{release_notes}

# Style examples from the user (study the tone and structure, do not copy the content)
{style_examples}
"""


@dataclass
class ContainerSnapshot:
    """All the facts the curator pulls out of a single running container."""

    name: str
    image: str
    image_digest_short: str
    repo: str | None
    current_version: str | None
    ports: list[str] = field(default_factory=list)
    mounts: list[str] = field(default_factory=list)
    env_vars: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class CurateResult:
    """Outcome of a single `Curator.curate_one` call."""

    snapshot: ContainerSnapshot
    status: str  # "written" | "skipped_same_digest" | "skipped_manual" | "skipped_dry_run" | "llm_failed"
    path: Path | None = None
    body: str | None = None
    note: str | None = None  # human-readable reason, optional


# ─── Helpers ─────────────────────────────────────────────────────────────


_SECRET_NAME_RE = re.compile(
    r"(token|key|password|secret|auth|credential|dsn|api[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)

_FOOTER_RE = re.compile(
    r"<!--\s*curator:\s*(?P<name>[^@\s]+)@(?P<digest>[A-Za-z0-9]+)\s*-->"
)

# Labels worth showing the LLM — everything else is noise (auto-generated
# Docker internals, build metadata, etc).
_LABEL_ALLOW_PREFIX = (
    "com.docker.compose.project",
    "com.docker.compose.service",
    "org.opencontainers.image.source",
    "org.opencontainers.image.version",
    "org.opencontainers.image.title",
    "homepage.",
    "net.unraid.docker.",
    "traefik.",
    "diun.",
)


def _redact_env(env: list[str]) -> list[str]:
    """Return env vars with secret-looking values replaced by `[REDACTED]`."""
    out: list[str] = []
    for entry in env or []:
        if "=" not in entry:
            out.append(entry)
            continue
        name, _, value = entry.partition("=")
        if _SECRET_NAME_RE.search(name):
            out.append(f"{name}=[REDACTED]")
        else:
            out.append(f"{name}={value}")
    return out


def _format_ports(attrs: dict[str, Any]) -> list[str]:
    """`"8080/tcp -> 0.0.0.0:8080"` style strings; empty list if none."""
    ports = (attrs.get("NetworkSettings") or {}).get("Ports") or {}
    out: list[str] = []
    for container_port, bindings in ports.items():
        if not bindings:
            continue
        for b in bindings:
            host = f"{b.get('HostIp', '')}:{b.get('HostPort', '')}".lstrip(":")
            out.append(f"{container_port} -> {host}")
    return out


def _format_mounts(attrs: dict[str, Any]) -> list[str]:
    """`"<source> -> <destination> (rw|ro)"` per mount."""
    out: list[str] = []
    for m in attrs.get("Mounts") or []:
        src = m.get("Source") or m.get("Name") or "?"
        dst = m.get("Destination") or "?"
        mode = "ro" if m.get("RW") is False else "rw"
        out.append(f"{src} -> {dst} ({mode})")
    return out


def _filter_labels(labels: dict[str, str] | None) -> dict[str, str]:
    if not labels:
        return {}
    return {k: v for k, v in labels.items() if k.startswith(_LABEL_ALLOW_PREFIX)}


def _digest_short(c: Container) -> str:
    """First 12 hex chars of the image id, sha256-prefix stripped."""
    image_id = (c.image.id or "").removeprefix("sha256:")
    return image_id[:12] or "unknown"


def _safe_filename(name: str) -> str:
    """Filesystem-safe `.md` filename for a container.

    Keep alphanumerics, dot, dash, underscore. Everything else → `_`.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._-") or "container"
    return f"{cleaned}.md"


def _strip_fences(text: str) -> str:
    """Drop ```markdown / ``` wrappers some models add despite the instruction."""
    s = text.strip()
    if s.startswith("```"):
        # First fence line: ```, ```markdown, ```md, …
        first_nl = s.find("\n")
        if first_nl > 0:
            s = s[first_nl + 1 :]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _existing_footer(text: str) -> tuple[str, str] | None:
    """Return `(name, digest)` from the curator footer, or None if absent."""
    m = _FOOTER_RE.search(text)
    if not m:
        return None
    return m.group("name"), m.group("digest")


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 16] + "\n…[truncated]…"


# ─── Curator class ───────────────────────────────────────────────────────


class _SafePromptDict(dict):
    """`str.format_map` helper that leaves unknown `{placeholders}` untouched.

    Lets custom prompt templates ignore placeholders they don't care about
    without raising KeyError.
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class Curator:
    """Generate Markdown notes for running containers.

    The curator is independent of the engine and the scheduler — it is invoked
    on demand from the CLI. Calling code is responsible for closing the Docker
    client when done (`Curator.close()`).
    """

    def __init__(
        self,
        cfg: CuratorConfig,
        llm: LLMClient,
        docker_cfg: DockerSourceConfig,
        notes_dir: str | Path,
    ):
        self.cfg = cfg
        self.llm = llm
        self.docker_cfg = docker_cfg
        # output_dir defaults to notes.notes_dir so the analyzer reads them back.
        out = cfg.output_dir or str(notes_dir)
        self.output_dir = Path(out).resolve() if out else None
        self._docker_plugin = DockerPlugin(docker_cfg)
        self._client: docker.DockerClient | None = None

    # ── Lifecycle ──

    def _docker_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.DockerClient(
                base_url=f"unix://{self.docker_cfg.socket.lstrip('/')}"
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    # ── Discovery ──

    def discover_targets(
        self,
        limit: int | None = None,
        only: list[str] | None = None,
    ) -> list[ContainerSnapshot]:
        """List running containers as `ContainerSnapshot` objects.

        Args:
            limit: optional cap on number of containers.
            only:  optional whitelist of container names — if set, only these
                   are returned, in the order provided.
        """
        client = self._docker_client()
        try:
            containers = client.containers.list(all=False)
        except Exception as e:
            log.error("Docker daemon unreachable at %s: %s", self.docker_cfg.socket, e)
            return []

        by_name: dict[str, Container] = {c.name: c for c in containers}
        if only:
            seen = []
            for name in only:
                c = by_name.get(name)
                if c is None:
                    log.warning("curator: container %r not running, skip", name)
                    continue
                seen.append(c)
            ordered = seen
        else:
            ordered = list(containers)
            if any(self._docker_plugin._should_skip(c.name) for c in ordered):  # noqa: SLF001
                ordered = [c for c in ordered if not self._docker_plugin._should_skip(c.name)]  # noqa: SLF001

        snapshots = [self._snapshot(c) for c in ordered]
        if limit is not None:
            snapshots = snapshots[:limit]
        return snapshots

    def _snapshot(self, c: Container) -> ContainerSnapshot:
        attrs = c.attrs or {}
        cfg_block = attrs.get("Config") or {}
        env = cfg_block.get("Env") or []
        labels = cfg_block.get("Labels") or {}
        image_tag = (c.image.tags[0] if c.image.tags else "") or cfg_block.get("Image", "")
        return ContainerSnapshot(
            name=c.name,
            image=image_tag,
            image_digest_short=_digest_short(c),
            repo=self._docker_plugin.resolve_repo(c),
            current_version=self._docker_plugin.extract_version(c),
            ports=_format_ports(attrs),
            mounts=_format_mounts(attrs),
            env_vars=_redact_env(env),
            labels=_filter_labels(labels),
        )

    # ── Note generation ──

    async def fetch_release_context(self, repo: str | None) -> str:
        """Concatenate the bodies of the N most recent releases, capped."""
        if not repo or self.cfg.recent_releases <= 0:
            return "(no upstream release notes available)"
        rels = await list_releases(repo, per_page=self.cfg.recent_releases)
        if not rels:
            return "(no upstream releases found)"
        parts: list[str] = []
        budget = self.cfg.max_release_chars
        for r in rels[: self.cfg.recent_releases]:
            tag = r.get("tag_name") or ""
            body = (r.get("body") or "").strip()
            chunk = f"## {tag}\n{body}\n".strip() + "\n"
            if budget <= 0:
                break
            chunk = chunk[:budget]
            budget -= len(chunk)
            parts.append(chunk)
        return "\n".join(parts).strip() or "(no upstream release notes available)"

    def load_template(self) -> str:
        """Read the configured custom template, falling back to the default."""
        path = self.cfg.prompt_template_path
        if not path:
            return DEFAULT_PROMPT_TEMPLATE
        p = Path(path)
        if not p.is_file():
            log.warning(
                "curator: prompt_template_path %r not found, using built-in default",
                path,
            )
            return DEFAULT_PROMPT_TEMPLATE
        return p.read_text(encoding="utf-8")

    def load_style_examples(self) -> str:
        """Concatenate configured style example files."""
        if not self.cfg.style_examples:
            return "(none provided)"
        parts: list[str] = []
        for ref in self.cfg.style_examples:
            p = Path(ref)
            if not p.is_file():
                log.warning("curator: style_example %r not found, skip", ref)
                continue
            parts.append(f"### {p.name}\n{p.read_text(encoding='utf-8')}")
        return "\n\n".join(parts) if parts else "(none provided)"

    def build_prompt(
        self,
        snapshot: ContainerSnapshot,
        release_notes: str,
        style_examples: str,
        template: str | None = None,
    ) -> str:
        """Render the prompt template with the snapshot data."""
        tpl = template if template is not None else self.load_template()
        ports = "\n  - " + "\n  - ".join(snapshot.ports) if snapshot.ports else "(none)"
        mounts = "\n  - " + "\n  - ".join(snapshot.mounts) if snapshot.mounts else "(none)"
        envs = "\n  - " + "\n  - ".join(snapshot.env_vars) if snapshot.env_vars else "(none)"
        if snapshot.labels:
            labels = "\n  - " + "\n  - ".join(f"{k}={v}" for k, v in snapshot.labels.items())
        else:
            labels = "(none)"
        values = _SafePromptDict(
            container_name=snapshot.name,
            image=snapshot.image or "(unknown)",
            repo=snapshot.repo or "(none)",
            current_version=snapshot.current_version or "(unknown)",
            ports=ports,
            mounts=mounts,
            env_vars=envs,
            labels=labels,
            release_notes=_truncate(release_notes, self.cfg.max_release_chars),
            style_examples=style_examples,
        )
        return tpl.format_map(values)

    # ── State on disk ──

    def _note_path(self, snapshot: ContainerSnapshot) -> Path | None:
        if self.output_dir is None:
            return None
        return self.output_dir / _safe_filename(snapshot.name)

    def _existing_state(self, snapshot: ContainerSnapshot) -> tuple[str, Path | None]:
        """Decide what to do with the existing file (if any).

        Returns one of:
          ("none", path)               — no file yet, safe to write.
          ("same_digest", path)        — already curated for this image.
          ("digest_changed", path)     — image rebuilt; regenerate.
          ("manual", path)             — file exists but no footer; do NOT overwrite.
        """
        path = self._note_path(snapshot)
        if path is None or not path.exists():
            return ("none", path)
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ("none", path)
        footer = _existing_footer(existing)
        if footer is None:
            return ("manual", path)
        _name, digest = footer
        if digest == snapshot.image_digest_short:
            return ("same_digest", path)
        return ("digest_changed", path)

    # ── Per-container entry point ──

    async def curate_one(
        self,
        snapshot: ContainerSnapshot,
        *,
        dry_run: bool = False,
        force: bool = False,
    ) -> CurateResult:
        if self.output_dir is None:
            return CurateResult(
                snapshot=snapshot,
                status="llm_failed",
                note="no output_dir configured (set curator.output_dir or notes.notes_dir)",
            )

        state, path = self._existing_state(snapshot)
        if not force:
            if state == "same_digest":
                return CurateResult(snapshot=snapshot, status="skipped_same_digest", path=path)
            if state == "manual":
                return CurateResult(snapshot=snapshot, status="skipped_manual", path=path)

        release_notes = await self.fetch_release_context(snapshot.repo)
        style_examples = self.load_style_examples()
        prompt = self.build_prompt(snapshot, release_notes, style_examples)

        body_raw = await self.llm.generate_text(prompt)
        if not body_raw:
            return CurateResult(snapshot=snapshot, status="llm_failed", note="LLM returned empty")
        body = _strip_fences(body_raw)
        footer = f"<!-- curator: {snapshot.name}@{snapshot.image_digest_short} -->"
        # If the LLM somehow emitted a stale footer, strip it before appending ours.
        body = _FOOTER_RE.sub("", body).rstrip()
        final = f"{body}\n\n{footer}\n"

        if dry_run:
            return CurateResult(
                snapshot=snapshot, status="skipped_dry_run", path=path, body=final
            )

        if path is None:
            return CurateResult(
                snapshot=snapshot,
                status="llm_failed",
                note="could not resolve output path",
                body=final,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(final, encoding="utf-8")
        tmp.replace(path)
        return CurateResult(snapshot=snapshot, status="written", path=path, body=final)
