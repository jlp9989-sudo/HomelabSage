"""Curator orchestrator: discovery, snapshot, prompt assembly, dedup, write."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import docker
from docker.models.containers import Container

from ..config import CuratorConfig, DockerSourceConfig
from ..db import Database
from ..enrichment import (
    Enrichment,
    fetch_container_logs,
    fetch_docker_hub_description,
    fetch_github_readme,
)
from ..github import list_releases
from ..llm import LLMClient
from ..models import InterviewQuestion
from ..plugins.docker import DockerPlugin
from .helpers import (
    FOOTER_RE,
    digest_short,
    existing_footer,
    filter_labels,
    format_mounts,
    format_ports,
    is_purpose_fallback,
    redact_env,
    safe_filename,
    strip_fences,
    truncate,
)
from .prompts import DEFAULT_PROMPT_TEMPLATE, SUGGESTION_PROMPT_TEMPLATE, SafePromptDict

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
    status: str  # "written" | "skipped_same_digest" | "skipped_manual" | "skipped_dry_run" | "llm_failed" | "interview_pending"
    path: Path | None = None
    body: str | None = None
    note: str | None = None  # human-readable reason, optional
    interview_question_id: int | None = None  # set when status == "interview_pending"


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
        db: Database | None = None,
    ):
        self.cfg = cfg
        self.llm = llm
        self.docker_cfg = docker_cfg
        # output_dir defaults to notes.notes_dir so the analyzer reads them back.
        out = cfg.output_dir or str(notes_dir)
        self.output_dir = Path(out).resolve() if out else None
        self._docker_plugin = DockerPlugin(docker_cfg)
        self._client: docker.DockerClient | None = None
        # Optional — when present, fallback (Rule 7) detections persist as
        # interview questions instead of writing a useless note. CLI usage
        # without a DB still works, just without the interview feature.
        self.db = db

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

    async def gather_enrichment(self, snapshot: ContainerSnapshot) -> Enrichment:
        """Collect README / Docker Hub / logs context for a container.

        Each source is independent; one failure never blocks the others.
        The two HTTP fetches run concurrently because they hit different
        hosts. Logs are sync (docker SDK) — runs after to keep the code
        readable; the savings would be tiny.
        """
        import asyncio as _asyncio

        readme_task = (
            fetch_github_readme(snapshot.repo, max_chars=self.cfg.max_readme_chars)
            if self.cfg.fetch_readme
            else _asyncio.sleep(0, result=None)
        )
        docker_hub_task = (
            fetch_docker_hub_description(
                snapshot.image, max_chars=self.cfg.max_docker_hub_chars
            )
            if self.cfg.fetch_docker_hub
            else _asyncio.sleep(0, result=None)
        )
        readme, docker_hub = await _asyncio.gather(readme_task, docker_hub_task)

        logs: str | None = None
        if self.cfg.include_logs:
            try:
                client = self._docker_client()
                container = client.containers.get(snapshot.name)
            except Exception:
                container = None
            logs = fetch_container_logs(
                container,
                tail=self.cfg.log_tail_lines,
                max_chars=self.cfg.max_logs_chars,
            )
        return Enrichment(readme=readme, docker_hub=docker_hub, logs=logs)

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

    def _base_values(
        self,
        snapshot: ContainerSnapshot,
        enrichment: Enrichment | None = None,
    ) -> SafePromptDict:
        """Build the snapshot-derived fields that both prompts share.

        Centralised so the main note prompt and the suggestion prompt see
        the same readme / docker hub / logs sections — no risk of one
        drifting from the other.
        """
        ports = "\n  - " + "\n  - ".join(snapshot.ports) if snapshot.ports else "(none)"
        mounts = "\n  - " + "\n  - ".join(snapshot.mounts) if snapshot.mounts else "(none)"
        envs = "\n  - " + "\n  - ".join(snapshot.env_vars) if snapshot.env_vars else "(none)"
        if snapshot.labels:
            labels = "\n  - " + "\n  - ".join(f"{k}={v}" for k, v in snapshot.labels.items())
        else:
            labels = "(none)"
        enr = enrichment or Enrichment()
        return SafePromptDict(
            container_name=snapshot.name,
            image=snapshot.image or "(unknown)",
            repo=snapshot.repo or "(none)",
            current_version=snapshot.current_version or "(unknown)",
            ports=ports,
            mounts=mounts,
            env_vars=envs,
            labels=labels,
            readme_excerpt=enr.readme or "(none)",
            docker_hub_description=enr.docker_hub or "(none)",
            recent_logs=enr.logs or "(none)",
        )

    def build_prompt(
        self,
        snapshot: ContainerSnapshot,
        release_notes: str,
        style_examples: str,
        template: str | None = None,
        user_purpose: str | None = None,
        enrichment: Enrichment | None = None,
    ) -> str:
        """Render the prompt template with the snapshot data.

        `user_purpose`, when supplied, is the user's verbatim answer to a
        prior interview question. The template's Rule 7 exception ensures
        the LLM uses it as the PURPOSE sentence and skips the fallback.

        `enrichment`, when supplied, fills the README / Docker Hub / logs
        sections. Each field is optional; missing sources render "(none)".
        """
        tpl = template if template is not None else self.load_template()
        values = self._base_values(snapshot, enrichment)
        values["release_notes"] = truncate(release_notes, self.cfg.max_release_chars)
        values["style_examples"] = style_examples
        values["user_purpose"] = (user_purpose or "").strip() or "(none provided)"
        return tpl.format_map(values)

    async def generate_suggestion(
        self,
        snapshot: ContainerSnapshot,
        enrichment: Enrichment | None = None,
    ) -> str | None:
        """Best-effort one-sentence guess used to prefill the interview answer.

        Returns None if the LLM declined ("(no guess)"), looped back into
        the Rule 7 fallback, or produced something obviously empty. The
        caller persists None as-is so the UI shows an empty textarea.
        """
        values = self._base_values(snapshot, enrichment)
        prompt = SUGGESTION_PROMPT_TEMPLATE.format_map(values)
        try:
            raw = await self.llm.generate_text(prompt)
        except Exception:
            log.exception("curator: suggestion LLM call failed for %s", snapshot.name)
            return None
        if not raw:
            return None
        text = strip_fences(raw).strip().strip("`'\"").strip()
        if not text:
            return None
        # The LLM may have emitted the explicit decline marker, or fallen
        # back into the Rule 7 wording despite this prompt being narrower.
        # In either case we have nothing useful to prefill with.
        if text.lower() == "(no guess)" or is_purpose_fallback(text):
            return None
        # First sentence only — some models still write a paragraph.
        first_line = text.splitlines()[0].strip()
        return first_line or None

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
        user_purpose: str | None = None,
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
        enrichment = await self.gather_enrichment(snapshot)
        prompt = self.build_prompt(
            snapshot,
            release_notes,
            style_examples,
            user_purpose=user_purpose,
            enrichment=enrichment,
        )

        body_raw = await self.llm.generate_text(prompt)
        if not body_raw:
            return CurateResult(snapshot=snapshot, status="llm_failed", note="LLM returned empty")
        body = strip_fences(body_raw)

        # Rule 7 fallback — the LLM gave up on inferring the purpose. Don't
        # write the useless note; instead persist an interview question (if
        # we have a DB) and let the user answer it. A re-curate with
        # `user_purpose` will skip this path.
        if is_purpose_fallback(body) and not user_purpose:
            # If the same question is already pending (cron re-run with no
            # rebuild), skip the suggestion LLM call — the user has the
            # earlier suggestion already, regenerating wastes ~5s per
            # container per run.
            already_pending = False
            if self.db is not None:
                existing = [
                    q
                    for q in self.db.list_interview_questions()
                    if q.container_name == snapshot.name
                    and q.image_digest_short == snapshot.image_digest_short
                ]
                already_pending = bool(existing)

            suggestion: str | None = None
            if not already_pending:
                # Best-effort: returns None on failure, the UI handles that.
                # Reuse the enrichment we already gathered for the main prompt.
                suggestion = await self.generate_suggestion(snapshot, enrichment)

            qid: int | None = None
            if self.db is not None:
                question = (
                    f"What is the purpose of `{snapshot.name}` in your homelab? "
                    "(One short sentence — used as the lead of its note.)"
                )
                qid = self.db.add_interview_question(
                    InterviewQuestion(
                        container_name=snapshot.name,
                        image_digest_short=snapshot.image_digest_short,
                        question_text=question,
                        suggested_text=suggestion,
                    )
                )
            return CurateResult(
                snapshot=snapshot,
                status="interview_pending",
                path=path,
                body=body,
                note="LLM emitted Rule 7 fallback — interview question pending",
                interview_question_id=qid,
            )

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
