"""Docker plugin — scan running containers, find GitHub repo, compare versions."""

from __future__ import annotations

import logging
import re

import docker
from docker.models.containers import Container
from packaging.version import InvalidVersion, Version

from ..config import DockerSourceConfig
from ..github import latest_release
from ..models import Update
from . import Plugin

log = logging.getLogger(__name__)

# `ghcr.io/owner/repo` or similar GHCR pattern
_GHCR_RE = re.compile(r"^(?:ghcr\.io|gcr\.io)/([\w.-]+)/([\w.-]+)")

# A tag "looks like a version" if it starts with optional v + digits.
# This rejects variant tags like "openvino", "cuda12", "ubuntu-22.04-full",
# "release-1.30.0", "alpine", which were causing false positives.
_SEMVER_RE = re.compile(r"^v?\d+(?:\.\d+){1,3}")


class DockerPlugin(Plugin):
    id = "docker"

    def __init__(self, cfg: DockerSourceConfig):
        self.cfg = cfg
        self._skip_re = [re.compile(p) for p in cfg.skip]

    def _client(self) -> docker.DockerClient:
        return docker.DockerClient(base_url=f"unix://{self.cfg.socket.lstrip('/')}")

    def _should_skip(self, name: str) -> bool:
        return any(p.search(name) for p in self._skip_re)

    def _find_github_repo(self, c: Container) -> str | None:
        """Resolve a container to `owner/repo` on GitHub.

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
            containers = client.containers.list(all=False)
        except Exception as e:
            log.error("Docker daemon unreachable at %s: %s", self.cfg.socket, e)
            return []

        for c in containers:
            if self._should_skip(c.name):
                continue
            repo = self._find_github_repo(c)
            if not repo:
                log.debug("no GitHub repo resolved for %s, skip", c.name)
                continue
            current = self._current_version(c)
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

            updates.append(
                Update(
                    source=self.id,
                    subject=c.name,
                    current_version=current,
                    new_version=new_version,
                    release_url=release.get("html_url"),
                    release_notes=release.get("body") or "",
                    context={
                        "image": image_tag,
                        "repo": repo,
                        "compose_project": compose_project,
                        "ports": list((c.attrs.get("NetworkSettings", {}).get("Ports") or {}).keys()),
                        "restart_policy": c.attrs.get("HostConfig", {}).get(
                            "RestartPolicy", {}
                        ).get("Name", ""),
                        # Forwarded to NotesProvider for relevance matching
                        "_note_keywords": keywords,
                    },
                )
            )
        return updates
