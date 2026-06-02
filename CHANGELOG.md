# Changelog

All notable changes ship here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely. Dates are UTC.

## v0.3.0 — 2026-06-02

A polish-pack release that fills out the detector / output layer and tightens
the engine. No breaking config changes from v0.2.x: every new knob is
opt-in and the existing YAML keeps working as-is.

### Added — sources

- **Watched repos plugin** (`sources.github_watched`). Track arbitrary
  GitHub or Codeberg repos that don't run as containers (toolboxes,
  scripts, dotfiles, firmware bundles). State lives in SQLite; manage via
  `homelabsage watched {add,list,toggle,remove}`. First scan after `add`
  seeds `current_version` to avoid a phantom 0→N update.
- **Compose dependency graph** (`sources.docker.compose_scan_paths`).
  Parses every `docker-compose.yml` under the configured roots and
  attaches `Update.context.cascade.depends_on_me` so the analyzer can
  warn about services that will also need to restart. Cached by mtime
  fingerprint — unchanged inputs return instantly.
- **CVE adapter** (`sources.docker.cve_scan`). Optional Trivy / Grype
  invocation per image; CRITICAL + HIGH counts and CVE IDs land in
  `Update.context.cve`. New prompt rule raises severity to `critical`
  when `counts.critical > 0`.
- **Image-size growth detector** (`sources.docker.image_size_growth_*`).
  Compares the new tag's Docker Hub manifest size against the local
  image; `Update.context.image_size_growth` flags ≥2× growth as a
  concrete bloatware signal.
- **PUID/PGID hint.** When a container declares either env var,
  `context.puid_pgid` carries the current value; a new analyzer rule
  surfaces it only when the release notes mention permission /
  uid / gid / rootless changes.

### Added — notifications

- **Discord webhook output.** Per-update embed with severity-coloured
  stripe. Configure via `outputs.discord.webhook_url`.
- **Ntfy output.** Plain-text POST to `<server>/<topic>` with severity
  → priority + tags + click URL. Works against ntfy.sh or self-hosted.
- **Gotify output.** App-token POST to `/message` with severity-based
  priority and optional per-severity overrides.
- **Weekly digest** (`digest:`). Sunday-09:00 rollup posted to every
  enabled channel and pinned to `notes/digest.md` for the curator. CLI
  via `homelabsage digest` / `--dry-run` / `--channel`.
- **Parity-aware notification gate** (`parity_gate:`). On Unraid + plain
  mdraid hosts, push outputs (Telegram / Discord / Ntfy / Gotify) skip
  while parity / resync / reshape is active. Skipped pushes are persisted
  in a new `pending_dispatches` table and auto-flushed on the next
  ungated scan. `parity_gate.skip_digest_too` opts the digest into the
  same gate. Manual replay: `homelabsage notify-pending --hours N`.
- **Five connection-test endpoints** under `/settings/.../test` — one per
  output. Each sends a minimal real payload so wrong tokens / wrong
  channels surface immediately.

### Added — curator + analyzer

- **System note curator (v0.4.1).** `homelabsage curate --system` writes
  `notes/system.md` from pluggable best-effort probes: kernel,
  `/etc/os-release`, `docker info`, CPU, RAM, GPUs (`nvidia-smi` /
  `rocm-smi`), ZFS pools, Unraid array status, proxy binaries on PATH.
  Idempotent via fingerprint footer; re-runs on unchanged hosts skip the
  write.
- **CSI mode.** `homelabsage csi <container>` — post-mortem assistant:
  pulls logs since the last detected update, filters to ERROR / WARN /
  FATAL, cross-references your notes, asks the LLM what likely broke
  and what to try. `--evidence-only` skips the LLM for air-gapped use.
- **On-demand URL analysis.** `homelabsage analyse <github-or-codeberg-url>`
  runs the same analyzer on any repo URL, no scan required.
- **Several new prompt rules.** The analyzer now reacts to `cve`,
  `puid_pgid`, `image_size_growth`, and `cascade.depends_on_me` context
  fields in addition to the existing `repo_health`, `alternatives`,
  `orphan_since_days` ones.

### Added — UI

- **`/diagnostics` page.** "What HomelabSage sees" — one row per
  container with verdict (tracked / floating_tag / no_repo / no_version /
  skipped_by_rule), reason, and notes. `/api/diagnostics` for scripting.

### Changed — quality

- **`config.py` split into a `config/` package.** Submodules per logical
  grouping (`llm`, `sources`, `outputs`, `runtime`, `storage`, `web`).
  Public surface preserved: `from homelabsage.config import …` keeps
  working unchanged.
- **`cli.py` split into a `cli/` package.** One file per command;
  `_common.py` owns the Typer app + shared option singletons. Entry
  point (`homelabsage = "homelabsage.cli:app"`) unchanged.
- **Async hygiene.** Trivy + compose-walk + diagnostics page now run
  blocking I/O via `asyncio.to_thread` so other plugins keep ticking on
  the event loop.
- **Compose graph cached** by aggregate mtime — unchanged inputs return
  the same instance instantly.

### Fixed

- Settings UI returned HTTP 500 instead of 400 when a Pydantic field
  validator raised `ValueError` (the exception object was not
  JSON-serialisable). Validator errors now stringify cleanly into the
  `validation_errors` response.

### Internal

- 550 → 705 tests, ruff clean, no new mypy errors (pre-existing patterns
  intact).
- New tables: `watched_repos`, `pending_dispatches`. Forward-only
  migrations triggered automatically on first run.

## v0.2.0 — 2026-05-12

Detector layer ground-up: floating-tag tracking, orphan containers,
abandoned-repo classification, alternative-image suggestions, curator
notes, interview mode for Rule 7 fallbacks. See git log for the granular
commit-by-commit history.

## v0.1.0 — 2026-05-10

Initial public release. Docker + Home Assistant plugins, Notion +
Telegram outputs, schema-driven settings UI, three-step first-run wizard.
