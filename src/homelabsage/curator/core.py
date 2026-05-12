"""Curator orchestrator: discovery, snapshot, prompt assembly, dedup, write."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import docker
from docker.models.containers import Container

from ..config import CuratorConfig, DockerSourceConfig
from ..github import list_releases
from ..llm import LLMClient
from ..plugins.docker import DockerPlugin
from .helpers import (
    FOOTER_RE,
    digest_short,
    existing_footer,
    filter_labels,
    format_mounts,
    format_ports,
    redact_env,
    safe_filename,
    strip_fences,
    truncate,
)
from .prompts import DEFAULT_PROMPT_TEMPLATE, SafePromptDict

log = logging.getLogger(__name__)


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
            image_digest_short=digest_short(c),
            repo=self._docker_plugin.resolve_repo(c),
            current_version=self._docker_plugin.extract_version(c),
            ports=format_ports(attrs),
            mounts=format_mounts(attrs),
            env_vars=redact_env(env),
            labels=filter_labels(labels),
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
        values = SafePromptDict(
            container_name=snapshot.name,
            image=snapshot.image or "(unknown)",
            repo=snapshot.repo or "(none)",
            current_version=snapshot.current_version or "(unknown)",
            ports=ports,
            mounts=mounts,
            env_vars=envs,
            labels=labels,
            release_notes=truncate(release_notes, self.cfg.max_release_chars),
            style_examples=style_examples,
        )
        return tpl.format_map(values)

    # ── State on disk ──

    def _note_path(self, snapshot: ContainerSnapshot) -> Path | None:
        if self.output_dir is None:
            return None
        return self.output_dir / safe_filename(snapshot.name)

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
        footer = existing_footer(existing)
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
        body = strip_fences(body_raw)
        footer = f"<!-- curator: {snapshot.name}@{snapshot.image_digest_short} -->"
        # If the LLM somehow emitted a stale footer, strip it before appending ours.
        body = FOOTER_RE.sub("", body).rstrip()
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
