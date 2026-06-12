"""Source-plugin configuration: Docker, Home Assistant, scripts, Fedora,
github_watched, plus untyped placeholders for future plugins."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DockerSourceConfig(BaseModel):
    enabled: bool = False
    socket: str = "/var/run/docker.sock"
    overrides: dict[str, str] = Field(default_factory=dict)
    skip: list[str] = Field(default_factory=list)
    # Containers `Status=exited` for at least this many days are flagged on each
    # Update via `context.orphan_since_days`. The analyzer prompt can pick that
    # up to suggest "is this container still needed?". Set to 0 to disable.
    orphan_min_days: int = 30
    # Look up alternative images on Docker Hub + LSIO and attach them to each
    # Update's context. Disabled by default — costs 2-3 HTTP calls per
    # container per scan (Docker Hub search + LSIO catalog + tag enrichment).
    # When the network is rate-limited or unreachable the call returns empty
    # and the scan continues normally. See `homelabsage.images.Criteria` for
    # the per-call thresholds (10x pulls ratio, 90-day freshness, …).
    find_alternatives: bool = False
    # Classify each repo as alive/stale/abandoned (GitHub `pushed_at` and
    # `archived` flag) and attach to Update.context.repo_health. Enabled by
    # default because it's one extra GET per Update we'd already emit — the
    # GitHub rate-limit budget covers this without raising costs. Set to
    # false if you want to skip the extra request.
    repo_health: bool = True
    # Walk every GitHub/Codeberg release strictly between the local tag
    # and the candidate tag, concatenate the bodies and attach to
    # `Update.context.release_notes_diff`. The analyzer prompt mines the
    # diff for breaking changes the user actually crosses by upgrading.
    # On by default — one extra paginated GET per update, cheap, but the
    # signal it produces is the project's main wedge over Watchtower/
    # Diun/WUD which only say "new tag exists".
    releases_diff: bool = True
    # Track containers whose tag is not a version (`latest`, `main`, `edge`,
    # `stable`, …) by comparing the local image digest with what the
    # registry currently serves under that tag. Only Docker Hub is queried
    # (anonymous v2 API); other hosts fall through silently. Saves the
    # ~10 containers per typical homelab that would otherwise be invisible.
    track_floating_tags: bool = True
    # Run an external CVE scanner (`trivy` then `grype`) on each container's
    # image and attach a CRITICAL/HIGH summary to `Update.context.cve`. No-op
    # when neither binary is on PATH. Off by default because trivy needs to
    # download/refresh its vulnerability DB on first run.
    cve_scan: bool = False
    # Directories to walk for `docker-compose.yml` files. When set, the
    # plugin enriches each Update with a `cascade` block listing services
    # that depend on it (via `depends_on` or shared networks). Empty list
    # disables the detector entirely — no scan, no extra context.
    compose_scan_paths: list[str] = Field(default_factory=list)
    # Bloatware-ish image-size growth detector. When a newer image manifest is
    # at least `bloatware_image_size_ratio` times the size of the current one,
    # the docker plugin attaches `context.image_size_growth` so the analyzer
    # can flag it. Off by default — opt-in because the manifest fetch costs
    # one extra HTTP roundtrip per update and most updates aren't bloatware.
    image_size_growth_detect: bool = False
    image_size_growth_ratio: float = Field(
        2.0,
        description="Trigger threshold: new_size_bytes / current_size_bytes ≥ ratio.",
    )
    # Will-it-fit cross-signal. When ON, the engine crosses the candidate
    # image's size (from `image_size_growth_detect`, which must also be on)
    # with the free space on `disk_pressure.paths`, and attaches
    # `Update.context.image_fit` when the pull would not fit / be tight.
    # Off by default — needs both inputs configured to mean anything; no
    # extra HTTP or subprocess (one `statvfs` per scan).
    image_fit_check: bool = False
    # Release-cadence stagnation: for each repo, compute the typical
    # days-between-releases over the last 30 releases and flag when
    # `now - latest_release` exceeds the historical median by ≥2× (medium)
    # or ≥4× (high). Catches "active dev branch, no releases for months"
    # which `repo_health` misses (it only looks at last push, not tag rate).
    # On by default — one extra paginated `list_releases` HTTP call per
    # update, cheap relative to the signal it produces. Set to false to
    # skip the request budget.
    release_cadence: bool = True
    # Detect sidecar / init containers tightly coupled to a primary.
    # When ON, the plugin walks every container's `HostConfig.NetworkMode`
    # / `PidMode` for `container:<name>` references and emits
    # `Update.context.sidecars` listing them on the PRIMARY's row. The
    # analyzer prompt mentions sidecars in the recommendation so the user
    # knows their VPN / init container will restart too. One bool, no
    # per-pattern config — the detection is mechanical.
    detect_sidecars: bool = True
    # Flag containers running without `mem_limit:` / `cpus:` set. One
    # unbounded leak can OOM the host; the seatbelt is one config line.
    # Attaches `Update.context.resource_limits` when either limit missing.
    detect_resource_limits: bool = True
    # Days-since-creation threshold. Containers older than this are
    # flagged in `Update.context.container_age` so the analyzer can
    # mention the cumulative drift (env vars, layer caches, drifted
    # permissions). Set to 0 to disable. Default 180 days.
    container_age_warn_after_days: int = 180
    # Restart-flapping detector — flag containers crashing at >0.25
    # restarts/hour. Useful pre-update signal: a flapping container
    # should be fixed BEFORE you change anything else.
    detect_restart_flapping: bool = True
    # Privileged-port + public-bind detector. Pure metadata read off
    # `NetworkSettings.Ports`; no extra HTTP calls.
    detect_exposed_ports: bool = True
    # Healthcheck-staleness detector. Catches `restart: always`
    # containers that are running but report `unhealthy` for hours
    # — restart_freq misses these (no restarts).
    detect_healthcheck_stale: bool = True
    # Compose `docker-compose.override.yml` detector. Flags
    # containers whose compose project has an override file so the
    # user knows the merged graph differs from what we see.
    detect_compose_override: bool = True
    # Restart-policy auditor. Default on — a container without an
    # explicit `restart:` becomes a silent-disappear bug after the
    # next reboot. Strict mode also flags bounded on-failure (off).
    detect_restart_policy: bool = True
    detect_restart_policy_strict: bool = False
    # OOM-killed detector. Default on — `State.OOMKilled` is a free
    # signal already in every `docker inspect` output.
    detect_oom_killed: bool = True
    # `--network=host` detector. Default on at `info` severity.
    detect_network_mode_host: bool = True


class HAConfig(BaseModel):
    enabled: bool = False
    url: str = Field(
        "http://homeassistant.local:8123",
        description="Base URL of Home Assistant. LAN: `http://homeassistant.local:8123` or `http://<ip>:8123`.",
    )
    token: str = Field(
        "",
        description=(
            "Long-lived access token. Generate in HA: click your profile (bottom-left) → "
            "Security tab → 'Long-lived access tokens' → Create."
        ),
    )
    include_hacs: bool = Field(
        True,
        description="Include HACS-managed integrations (depends on the HACS sensor being exposed).",
    )
    include_addons: bool = Field(
        True,
        description="Include HA OS add-ons in the scan (requires Supervisor; non-OS installs return empty).",
    )


class ScriptsSourceConfig(BaseModel):
    """Enumerate cron jobs / systemd timers / Unraid User Scripts on the host.

    Universal across distros: every probe gracefully no-ops when its target
    directory or binary is missing, so the same defaults work on Debian,
    Fedora, Arch, Alpine, Unraid, macOS, etc.

    `etc_root` / `cron_spool` / `unraid_scripts_root` exist for the rare
    case where the relevant directories live under a non-default prefix
    (e.g. a chroot or a mounted host filesystem at `/host/etc`).
    """

    enabled: bool = False
    etc_root: str = "/etc"
    cron_spool: str = "/var/spool/cron"
    unraid_scripts_root: str = "/boot/config/plugins/user.scripts/scripts"
    systemctl_path: str = "systemctl"
    enable_systemd: bool = True
    enable_unraid: bool = False


class FedoraSourceConfig(BaseModel):
    """Pull dnf updates from a Fedora host over SSH.

    The host needs nothing special — just `dnf check-update --refresh` on
    a SSH-reachable account. The plugin parses the standard 3-column
    output and emits one `Update` per matched package.

    Filtering rationale: a typical Fedora server has 30-150 packages
    pending at any given time. Sending each one through the analyzer
    would be expensive and the result would be 90 % noise (random
    userspace libs). Instead, packages matched by `critical_packages`
    become individual updates; the rest are rolled up into ONE summary
    update so the user still sees the total count without LLM cost.
    """

    enabled: bool = Field(
        False,
        description="Enable the Fedora plugin. Off by default — it needs SSH credentials.",
    )
    host: str = Field("", description="SSH host or IP (e.g. `192.168.31.19`).")
    user: str = Field(
        "",
        description="SSH user on the target host. The user must be able to run "
        "`dnf check-update` (no sudo required — dnf reads metadata as any user).",
    )
    ssh_key_path: str = Field(
        "",
        description="Path INSIDE the homelabsage container to the private key. "
        "Mount your key read-only via the compose file, e.g. "
        "`/mnt/user/appdata/.../halo_ed25519:/opt/ssh-keys/halo:ro`.",
        json_schema_extra={"ui_widget": "path", "ui_path_kind": "file"},
    )
    port: int = 22
    known_hosts_path: str = Field(
        "",
        description="Optional known_hosts file. Leave empty to accept the host "
        "key on first connection (logged at WARNING).",
        json_schema_extra={"ui_widget": "path", "ui_path_kind": "file"},
    )
    critical_packages: list[str] = Field(
        default_factory=lambda: [
            r"^kernel(-core|-modules|-modules-core|-modules-extra|-devel)?$",
            r"^linux-firmware$",
            r"^mesa-",
            r"^vulkan-",
            r"^rocm-",
            r"^llvm",
            r"^gcc$",
            r"^glibc(-common|-langpack-.+)?$",
            r"^systemd(-libs|-pam|-udev|-resolved|-networkd)?$",
            r"^podman$",
            r"^docker(-ce)?$",
            r"^dnf$",
            r"^selinux-policy(-targeted)?$",
            r"^openssh(-server|-clients)?$",
        ],
        description="Regex list. Packages whose name matches ANY pattern become "
        "individual `Update` items (and get LLM-analyzed). Everything else is "
        "aggregated into a single summary entry.",
    )
    aggregate_others: bool = Field(
        True,
        description="If true, non-critical pending packages are reported as ONE "
        "summary update (`fedora-userspace`). If false, they're dropped silently.",
    )
    timeout_seconds: int = Field(
        45,
        description="SSH + dnf metadata refresh can take a while on cold caches.",
    )


class GitHubWatchedConfig(BaseModel):
    """Plugin that scans arbitrary user-curated GitHub/Codeberg repos.

    No knobs beyond `enabled` — the actual list is per-row in SQLite, managed
    via `homelabsage watched ...`. Keeping the configuration in the DB rather
    than YAML lets non-technical users add/remove repos without editing
    files."""

    enabled: bool = False


class SourcesConfig(BaseModel):
    docker: DockerSourceConfig = Field(default_factory=DockerSourceConfig)
    homeassistant: HAConfig = Field(default_factory=HAConfig)
    scripts: ScriptsSourceConfig = Field(default_factory=ScriptsSourceConfig)
    fedora: FedoraSourceConfig = Field(default_factory=FedoraSourceConfig)
    github_watched: GitHubWatchedConfig = Field(default_factory=GitHubWatchedConfig)
    # Placeholders for future plugins — kept loose to not break configs.
    llamacpp: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    huggingface_models: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    unraid: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    rss_feeds: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
