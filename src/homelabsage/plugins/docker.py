"""Docker plugin — scan running containers, find GitHub repo, compare versions."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime

import docker
from docker.models.containers import Container
from packaging.version import InvalidVersion, Version

from .._time import parse_docker_ts
from ..compose import DependencyGraph
from ..compose import build_graph as build_compose_graph
from ..config import DockerSourceConfig
from ..cve import scan_image as scan_image_cve
from ..github import classify_repo_health, latest_release, repo_metadata
from ..image_size import detect_growth as detect_image_growth
from ..images import find_alternatives
from ..models import Update
from ..registries import (
    dockerhub_tag_info,
    local_digest_for,
    parse_image_ref,
)
from ..releases_diff import build_diff as build_releases_diff
from . import Plugin

log = logging.getLogger(__name__)


def _orphan_days(
    status: str,
    finished_at: str,
    *,
    now: datetime | None = None,
) -> int | None:
    """Days a container has been in the `exited` state, or None if not orphaned.

    The check is intentionally narrow: we only flag containers Docker has
    actually marked exited (manual stop counts) and that have a parseable
    `FinishedAt`. `created` / `paused` / `restarting` / `dead` are not
    considered orphans — those need human attention faster than a 30-day window.
    """
    if status != "exited":
        return None
    finished = parse_docker_ts(finished_at)
    if finished is None:
        return None
    now = now or datetime.now(UTC)
    return max(0, int((now - finished).total_seconds() // 86400))

# `ghcr.io/owner/repo` or similar GHCR pattern
_GHCR_RE = re.compile(r"^(?:ghcr\.io|gcr\.io)/([\w.-]+)/([\w.-]+)")

# A tag "looks like a version" if it starts with optional v + digits.
# This rejects variant tags like "openvino", "cuda12", "ubuntu-22.04-full",
# "release-1.30.0", "alpine", which were causing false positives.
_SEMVER_RE = re.compile(r"^v?\d+(?:\.\d+){1,3}")


def _looks_like_semver(s: str | None) -> bool:
    """True iff `s` is a non-empty version string we can meaningfully compare.

    Used by the floating-tag branch to detect cases where `_current_version`
    fell back to an OCI label whose value is itself opaque (e.g. some
    images set `org.opencontainers.image.version=latest`). In those cases
    we don't have a real version to compare, so the digest branch should
    take over.
    """
    return bool(s) and _SEMVER_RE.match(s or "") is not None


class DockerPlugin(Plugin):
    id = "docker"

    def __init__(self, cfg: DockerSourceConfig):
        self.cfg = cfg
        self._skip_re = [re.compile(p) for p in cfg.skip]

    def _client(self) -> docker.DockerClient:
        return docker.DockerClient(base_url=f"unix://{self.cfg.socket.lstrip('/')}")

    def _should_skip(self, name: str) -> bool:
        return any(p.search(name) for p in self._skip_re)

    def resolve_repo(self, c: Container) -> str | None:
        """Public alias of `_find_github_repo` — used by the curator."""
        return self._find_github_repo(c)

    def extract_version(self, c: Container) -> str | None:
        """Public alias of `_current_version` — used by the curator."""
        return self._current_version(c)

    def _find_github_repo(self, c: Container) -> str | None:
        """Resolve a container to a `repo` identifier the GitHub helper accepts.

        Returned shapes:
          - `owner/repo`               → GitHub (default).
          - `codeberg.org/owner/repo`  → Codeberg (runs Forgejo, exposes
                                         a GitHub-compatible REST API at
                                         `/api/v1`). github.py routes by
                                         prefix.

        Priority:
          1) `overrides` in config (container_name → repo).
          2) OCI image label `org.opencontainers.image.source`.
          3) Heuristic on image ref (ghcr.io/owner/repo).
        """
        # 1. Override
        if repo := self.cfg.overrides.get(c.name):
            return repo

        # 2. OCI label
        try:
            image = c.image
            labels = image.labels or {}
            src = labels.get("org.opencontainers.image.source", "")
            if "github.com/" in src:
                # https://github.com/owner/repo → owner/repo
                tail = src.split("github.com/", 1)[1].strip("/")
                parts = tail.split("/")
                if len(parts) >= 2:
                    return f"{parts[0]}/{parts[1]}"
            if "codeberg.org/" in src:
                # https://codeberg.org/owner/repo → codeberg.org/owner/repo
                tail = src.split("codeberg.org/", 1)[1].strip("/")
                parts = tail.split("/")
                if len(parts) >= 2:
                    return f"codeberg.org/{parts[0]}/{parts[1]}"
        except Exception:
            pass

        # 3. Heuristic from image tag
        image_ref = (c.image.tags[0] if c.image.tags else "") or c.attrs.get("Config", {}).get(
            "Image", ""
        )
        if m := _GHCR_RE.match(image_ref):
            return f"{m.group(1)}/{m.group(2)}"

        return None

    def _current_version(self, c: Container) -> str | None:
        """Best-effort extraction of running version.

        Strategies (first match wins):
          1. Image tag that LOOKS LIKE a version (matches semver-ish regex).
             This skips variant tags like `openvino`, `cuda`, `release-1.x`
             that aren't real versions.
          2. OCI label `org.opencontainers.image.version`.
        """
        for tag in c.image.tags or []:
            after = tag.split(":")[-1] if ":" in tag else ""
            if after and _SEMVER_RE.match(after):
                return after.lstrip("v")
        try:
            labels = c.image.labels or {}
            if v := labels.get("org.opencontainers.image.version"):
                return v.lstrip("v")
        except Exception:
            pass
        return None

    @staticmethod
    def _is_newer(current: str, candidate: str) -> bool:
        """True if `candidate` is strictly greater than `current`.

        Refuses to compare when either side isn't real semver — string-`!=`
        produces too many false positives (`"openvino" != "2.7.5"`, etc).
        """
        try:
            return Version(candidate.lstrip("v")) > Version(current.lstrip("v"))
        except InvalidVersion:
            return False

    async def scan(self) -> list[Update]:
        if not self.cfg.enabled:
            return []
        updates: list[Update] = []
        try:
            client = self._client()
            # all=True so orphan (exited) containers also surface — without this,
            # a long-stopped container with a pending CVE just disappears.
            containers = client.containers.list(all=True)
        except Exception as e:
            log.error("Docker daemon unreachable at %s: %s", self.cfg.socket, e)
            return []

        # Build the compose graph ONCE per scan — it's read-only and the
        # walk cost (a handful of YAML files) is fixed regardless of how
        # many containers we iterate below. Filesystem walk runs off the
        # event loop so concurrent HTTP plugins keep ticking.
        compose_graph: DependencyGraph | None = None
        if self.cfg.compose_scan_paths:
            try:
                compose_graph = await asyncio.to_thread(
                    build_compose_graph, self.cfg.compose_scan_paths
                )
                log.debug(
                    "compose graph: %d services from %s",
                    len(compose_graph.services), self.cfg.compose_scan_paths,
                )
            except Exception as e:
                log.warning("compose graph build failed: %s", e)

        # Sidecar discovery runs ONCE per scan — same shape as the
        # compose graph cache. Empty list on any failure.
        sidecar_links: list = []
        if self.cfg.detect_sidecars:
            try:
                from ..sidecars import (
                    discover_from_compose_graph,
                    discover_runtime,
                )
                sidecar_links = discover_runtime(containers)
                if compose_graph is not None:
                    sidecar_links.extend(discover_from_compose_graph(compose_graph))
            except Exception as e:
                log.debug("sidecar discovery failed: %s", e)

        for c in containers:
            if self._should_skip(c.name):
                continue
            repo = self._find_github_repo(c)
            current = self._current_version(c)

            # Floating-tag branch: when there's no usable semver to compare
            # against (no tag, or the OCI image.version label is something
            # opaque like "latest"), fall back to comparing the local image
            # digest against what Docker Hub serves for the same tag. The
            # result lacks release notes but still surfaces "this container
            # is out of date" — the analyzer says `apply` or `hold` based
            # on the user's notes.
            if self.cfg.track_floating_tags and not _looks_like_semver(current):
                floating = await self._floating_tag_update(c)
                if floating is not None:
                    updates.append(floating)
                    continue
            if not repo:
                log.debug("no GitHub repo resolved for %s, skip", c.name)
                continue
            if not current:
                log.debug("no parseable version for %s (image tag %s), skip",
                          c.name, c.image.tags)
                continue
            release = await latest_release(repo)
            if not release:
                continue
            new_version = release.get("tag_name", "").lstrip("v")
            if not new_version or not self._is_newer(current, new_version):
                continue
            labels = c.attrs.get("Config", {}).get("Labels") or {}
            compose_project = labels.get("com.docker.compose.project", "")
            image_tag = c.image.tags[0] if c.image.tags else ""
            # Short image name for note matching: "owner/name:tag" → "name"
            image_short = image_tag.split("/")[-1].split(":")[0]
            keywords = [k for k in {repo, repo.split("/")[-1], image_short, compose_project} if k]

            state = c.attrs.get("State", {}) or {}
            ctx: dict[str, object] = {
                "image": image_tag,
                "repo": repo,
                "compose_project": compose_project,
                "ports": list((c.attrs.get("NetworkSettings", {}).get("Ports") or {}).keys()),
                "restart_policy": c.attrs.get("HostConfig", {}).get(
                    "RestartPolicy", {}
                ).get("Name", ""),
                "_note_keywords": keywords,
            }
            if self.cfg.orphan_min_days > 0:
                days = _orphan_days(
                    str(state.get("Status", "")),
                    str(state.get("FinishedAt", "")),
                )
                if days is not None and days >= self.cfg.orphan_min_days:
                    ctx["orphan_since_days"] = days

            if self.cfg.find_alternatives and image_tag:
                try:
                    alts = await find_alternatives(
                        image_tag,
                        description=labels.get(
                            "org.opencontainers.image.description", ""
                        ),
                    )
                    if alts.candidates:
                        ctx["alternatives"] = [a.to_context() for a in alts.candidates]
                except Exception as e:
                    log.debug("find_alternatives failed for %s: %s", c.name, e)

            # PUID/PGID hint: if the container declares either env var,
            # surface it so the analyzer prompt rule can warn on rename or
            # behavioural changes upstream (LSIO images, rootless rebases).
            env_list = (c.attrs.get("Config", {}) or {}).get("Env") or []
            puid_pgid = {
                "PUID": next(
                    (e.split("=", 1)[1] for e in env_list if e.startswith("PUID=")),
                    None,
                ),
                "PGID": next(
                    (e.split("=", 1)[1] for e in env_list if e.startswith("PGID=")),
                    None,
                ),
            }
            if any(v is not None for v in puid_pgid.values()):
                ctx["puid_pgid"] = {k: v for k, v in puid_pgid.items() if v is not None}

            if self.cfg.releases_diff:
                try:
                    diff = await build_releases_diff(repo, current, new_version)
                    if diff is not None:
                        ctx["release_notes_diff"] = diff.to_context()
                except Exception as e:
                    log.debug("releases_diff failed for %s: %s", repo, e)

            if self.cfg.repo_health:
                try:
                    meta = await repo_metadata(repo)
                    health = classify_repo_health(meta)
                    # Only emit when we have a usable signal — "unknown" adds noise.
                    if health.get("status") != "unknown":
                        ctx["repo_health"] = health
                except Exception as e:
                    log.debug("repo_health failed for %s: %s", repo, e)

            if self.cfg.release_cadence:
                try:
                    from ..github import list_releases
                    from ..release_cadence import detect_stagnation
                    rels = await list_releases(repo, per_page=30)
                    finding = detect_stagnation(rels)
                    if finding is not None:
                        ctx["release_cadence"] = finding.to_context()
                except Exception as e:
                    log.debug("release_cadence failed for %s: %s", repo, e)

            if compose_graph is not None:
                deps = compose_graph.dependents_of(c.name)
                neighbours = compose_graph.network_neighbours_of(c.name)
                if deps or neighbours:
                    ctx["cascade"] = {
                        "depends_on_me": [
                            {"service": d.name, "project": d.project}
                            for d in deps[:20]
                        ],
                        "network_neighbours": [
                            {"service": n.name, "project": n.project}
                            for n in neighbours[:20]
                        ],
                    }

            if sidecar_links:
                from ..sidecars import sidecars_for
                matches = sidecars_for(c.name, sidecar_links)
                if matches:
                    ctx["sidecars"] = [link.to_context() for link in matches]

            if self.cfg.detect_resource_limits:
                from ..resource_limits import evaluate as eval_limits
                rl = eval_limits(c.attrs)
                if rl is not None:
                    ctx["resource_limits"] = rl.to_context()

            if self.cfg.container_age_warn_after_days > 0:
                from ..container_age import evaluate as eval_age
                age = eval_age(
                    c.attrs,
                    warn_after_days=self.cfg.container_age_warn_after_days,
                )
                if age is not None:
                    ctx["container_age"] = age.to_context()

            if self.cfg.detect_restart_flapping:

                from .._time import utcnow
                from ..restart_freq import evaluate as eval_restart
                state = c.attrs.get("State") or {}
                started_at = parse_docker_ts(state.get("StartedAt") or "")
                rc = int(c.attrs.get("RestartCount") or 0)
                rf = eval_restart(
                    restart_count=rc, started_at=started_at, now=utcnow(),
                )
                if rf is not None:
                    ctx["restart_freq"] = rf.to_context()

            if self.cfg.detect_exposed_ports:
                from ..exposed_ports import evaluate as eval_ports
                pf = eval_ports(c.attrs.get("NetworkSettings") or {})
                if pf:
                    ctx["exposed_ports"] = [p.to_context() for p in pf]

            if self.cfg.detect_healthcheck_stale:
                from .._time import utcnow
                from ..healthcheck_stale import evaluate as eval_health
                hs = eval_health(
                    c.attrs.get("State") or {}, now=utcnow(),
                )
                if hs is not None:
                    ctx["healthcheck_stale"] = hs.to_context()

            if self.cfg.detect_restart_policy:
                from ..restart_policy import evaluate as eval_rp
                rp = eval_rp(
                    c.attrs.get("HostConfig") or {},
                    strict=self.cfg.detect_restart_policy_strict,
                )
                if rp is not None:
                    ctx["restart_policy"] = rp.to_context()

            if self.cfg.detect_oom_killed:
                from ..oom_killed import evaluate as eval_oom
                oom = eval_oom(c.attrs.get("State") or {})
                if oom is not None:
                    ctx["oom_killed"] = oom.to_context()

            if self.cfg.detect_network_mode_host:
                from ..network_mode_host import evaluate as eval_hostnet
                hn = eval_hostnet(c.attrs.get("HostConfig") or {})
                if hn is not None:
                    ctx["network_mode_host"] = hn.to_context()

            if self.cfg.image_size_growth_detect and image_tag:
                # Local image's on-disk size (sum of writeable + layer cache).
                # `c.image.attrs["Size"]` is set by `docker inspect`; falsy
                # when the SDK can't reach the daemon or the image is gone.
                try:
                    local_size = int((c.image.attrs or {}).get("Size") or 0)
                except (TypeError, ValueError):
                    local_size = 0
                if local_size > 0 and new_version:
                    try:
                        # The new tag for the same slug is `:<new_version>`;
                        # Docker Hub tag-info covers most images. Non-Hub
                        # registries fall through silently.
                        growth = await detect_image_growth(
                            image_tag,
                            local_size,
                            new_version,
                            threshold_ratio=self.cfg.image_size_growth_ratio,
                        )
                        if growth is not None:
                            ctx["image_size_growth"] = growth.to_context()
                    except Exception as e:
                        log.debug(
                            "image-size probe failed for %s: %s", c.name, e
                        )

            if self.cfg.cve_scan and image_tag:
                try:
                    # Trivy/grype are subprocess calls and can take 30-90s on
                    # cold DB cache; run off the event loop so we don't
                    # starve other plugins' HTTP work running concurrently.
                    summary = await asyncio.to_thread(scan_image_cve, image_tag)
                    if summary is not None:
                        ctx["cve"] = summary.to_context()
                except Exception as e:
                    log.debug("cve scan failed for %s: %s", c.name, e)

            updates.append(
                Update(
                    source=self.id,
                    subject=c.name,
                    current_version=current,
                    new_version=new_version,
                    release_url=release.get("html_url"),
                    release_notes=release.get("body") or "",
                    context=ctx,
                )
            )
        return updates

    async def _floating_tag_update(self, c: Container) -> Update | None:
        """Compare the local image digest with Docker Hub's `:tag` digest.

        Triggers when the container's tag isn't a version (so the semver
        path bailed). Quietly returns None for any failure mode — registry
        offline, non-Hub host, anonymous pull rate-limited, missing
        RepoDigests on a locally-built image — so the scan loop keeps going.

        Why this is worth its own branch instead of forcing semver: with
        Docker, plenty of stable images (and most "edge" images) only ever
        publish `:latest` or `:main`. Without this check they'd stay
        invisible to the analyzer.
        """
        image_ref_str = (c.image.tags[0] if c.image.tags else "") or (
            c.attrs.get("Config", {}).get("Image", "") or ""
        )
        ref = parse_image_ref(image_ref_str)
        if ref is None or not ref.is_docker_hub:
            return None
        # `c.image.attrs` is where RepoDigests live; `c.attrs["Image"]` is
        # the image SHA string, not the image config block.
        try:
            image_attrs = c.image.attrs or {}
        except Exception:
            image_attrs = {}
        local_digest = local_digest_for(image_ref_str, image_attrs)
        if not local_digest:
            return None

        try:
            remote = await dockerhub_tag_info(ref.slug, ref.tag)
        except Exception as e:
            log.debug("dockerhub query failed for %s: %s", image_ref_str, e)
            return None
        if remote is None or not remote.digest:
            return None
        if remote.digest == local_digest:
            return None

        # Pull image creation time off the inspected image config so the
        # analyzer can reason about age — "container was pulled 6 months
        # ago, registry digest changed last week" carries different weight
        # than "pulled yesterday, registry moved today".
        local_pulled_at = parse_docker_ts(str(image_attrs.get("Created") or ""))

        labels = c.attrs.get("Config", {}).get("Labels") or {}
        compose_project = labels.get("com.docker.compose.project", "")
        image_short = image_ref_str.split("/")[-1].split(":")[0]
        keywords = [
            k for k in {ref.slug, ref.slug.split("/")[-1], image_short, compose_project} if k
        ]

        ctx: dict[str, object] = {
            "image": image_ref_str,
            "registry": "docker.io",
            "registry_slug": ref.slug,
            "tag": ref.tag,
            "compose_project": compose_project,
            "_note_keywords": keywords,
            "remote_pushed_at": remote.pushed_at.isoformat() if remote.pushed_at else None,
            "local_pulled_at": local_pulled_at.isoformat() if local_pulled_at else None,
        }
        # Short digests (12-char) for human-readable subject/version fields.
        local_short = local_digest.removeprefix("sha256:")[:12]
        remote_short = remote.digest.removeprefix("sha256:")[:12]
        # Build a release_notes-equivalent block so the LLM still gets
        # context even without an upstream changelog.
        release_notes = (
            f"Tag `{ref.tag}` on docker.io/{ref.slug} now points at "
            f"a different image than the one this container is running.\n\n"
            f"- local digest:  {local_short}\n"
            f"- remote digest: {remote_short}\n"
        )
        if remote.pushed_at:
            release_notes += f"- registry push: {remote.pushed_at.isoformat()}\n"
        if local_pulled_at:
            release_notes += f"- local pull:    {local_pulled_at.isoformat()}\n"

        return Update(
            source=self.id,
            subject=c.name,
            current_version=f"local @ {local_short}",
            new_version=f"registry @ {remote_short}",
            release_url=f"https://hub.docker.com/r/{ref.slug}/tags",
            release_notes=release_notes,
            context=ctx,
        )
