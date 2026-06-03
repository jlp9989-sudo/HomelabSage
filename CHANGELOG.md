# Changelog

All notable changes ship here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely. Dates are UTC.

## v0.4.4 — 2026-06-03

Three operations features + a polish pass triggered by a code-review
agent that surfaced real bugs in the v0.4.3 batch.

### Added — operations

- **Stack-level rollback recipe generator** (`rollback.py`). For any
  `AnalyzedUpdate`, emits a copy-pasteable Markdown body with two
  options: docker compose (when a compose file backs the container,
  via the cascade detector's graph) and the universal docker CLI form.
  Annotates with cascade warnings — services that `depends_on:` the
  rolled-back one and therefore also need a restart. Pure data, never
  applies anything: a broken-update incident is the wrong moment for
  the tool to take destructive action without explicit user input.
- **Compose-file update-diff generator** (`compose_diff.py`). Shows
  the user exactly which line of exactly which compose file would
  change to apply an update. Text-level swap (not YAML round-trip)
  so comments, indentation and quoting style stay intact. Renders as
  a unified diff inside a Markdown ```diff block.
- **HACS Python-bump cascade detector** (`hacs_cascade.py`). When the
  HA plugin emits an `Update` for `core`, fetches
  `homeassistant/package_constraints.txt` at both the old and new
  release tags from raw.githubusercontent.com, extracts the
  `python_requires` floor, and attaches
  `context.hacs_python_bump = {from: "3.12", to: "3.13", ...}` when
  the floor moved. The analyzer prompt picks the signal up the same
  way it does CVE / cascade / image-size-growth context.

### Fixed (review-driven polish)

- **secret_guard KV regex missed bare `PASSWORD=` / `TOKEN=` /
  `API_KEY=`** — the prefix requirement was `[A-Z][A-Z0-9_]{1,}`,
  which forced at least one char before the literal suffix. Bare
  `KEY=value` lines silently escaped. Now matches both bare and
  namespaced forms.
- **tag_lag `and` vs `or` bug** — context with only `local_pulled_at`
  (no remote) returned a misleading `TagLag` instead of None. Now
  early-returns when no remote-push timestamp is available.
- **health_check / backup_health crashed on malformed timestamps** —
  `parse_iso` raises by design; the callers were missing the
  try/except so a single corrupt row could nuke the whole probe loop.
- **HF URL false-positives on `/datasets`, `/spaces`, `/papers`, etc.**
  — produced garbage slugs. Now reject the well-known HF non-model
  prefixes before returning.
- **HF safetensors int-vs-string handling** — large param counts are
  string-marshalled in some HF JSON responses; the `isinstance(v, int)`
  filter dropped them silently and reported `params=0`. Now converts
  with try/except so both shapes work.
- **HF quant detection used `safetensors.total`** which is the
  parameter count, not a quant marker. The first sibling-filename
  check now wins instead of being shadowed by meaningless coercion.
- **log_anomaly skip regexes recompiled per container** — now
  compiled once per scan.
- **secret_guard env-dict walk** — explicit pass that redacts known
  secret keys inside `context["env"]` even when the value doesn't
  match the credential pattern (covers `env.GITHUB_TOKEN=plain-text`).

### Internal

- 971 → 1004 tests (+33), ruff clean, 0 mypy errors against 112 files.
- 3 new modules + 3 new test files. No new config blocks (the new
  modules surface via existing pipelines — `rollback` reads
  `compose.DependencyGraph`, `compose_diff` reads the same, HACS
  cascade is wired into the HA plugin's `_scan_core`).

## v0.4.3 — 2026-06-03

Five safety + correctness features in one batch, opt-in via their own
config blocks. The theme: catching footguns before they fire.

### Added — safety

- **Pre-LLM secret-leak guard** (`secret_guard.py`). Every prompt about
  to be sent to a cloud LLM is scanned for API keys, OAuth tokens, SSH
  private key blocks, AWS access keys, Slack/Discord/Telegram webhook
  URLs, and `KEY=value` lines whose key matches a secret marker. Each
  match is replaced with `<redacted-by-homelabsage>` before assembly.
  On by default for cloud providers (openai/groq/gemini/openrouter/
  anthropic), off by default for local providers (ollama/disabled).
  Override per-profile via `llm.secret_guard`. Composes with the
  existing `redact.py` (export sanitiser) — different audiences,
  different aggressiveness.
- **Pre-flight breaking-change gate** (`web.preflight_gate`, new
  `/updates/<id>/preflight` template). Off by default. When on,
  clicking "Applied" on an update whose analysis carries non-empty
  `breaking_changes` redirects to a confirmation page listing the
  breaking lines + recommended action; an explicit ack POST flips the
  status. Dismiss flow unchanged.

### Added — detectors

- **Compose-file linter** (`compose_lint.py`, `compose_lint.enabled`).
  Walks the same paths as the cascade detector
  (`sources.docker.compose_scan_paths`) and emits findings: deprecated
  `links:`, `:latest` (or implicit-latest) image tag, missing
  `healthcheck:`, missing `restart:`, `privileged: true` without
  `cap_add:`, `network_mode: host`, LSIO PUID/PGID gotcha (bind to
  `/config` or `/data` without `user:`). Surfaced under `compose_lint`
  category in the auditor.
- **Tag-promotion-lag detector** (`tag_lag.py`, `tag_lag.enabled`).
  For floating-tag containers (`latest` / `main` / `stable`), derives
  `days_local_behind` from `remote_pushed_at` − `local_pulled_at`
  (both attached by the existing `track_floating_tags` pipeline) and
  emits a `medium` finding after 14 days, `high` after 60.

### Added — surfaces

- **HuggingFace model URL analyser** — sub-case (d) of the URL
  analyser. `homelabsage analyse https://huggingface.co/Owner/Model`
  fetches the model card, estimates VRAM (params × bytes-per-param,
  + 15% KV-cache headroom; quantisation detected from sibling
  filenames or the reported `safetensors` block), cross-references
  with `system.md` (the curator's host probe) and emits a fit
  verdict — `fits` / `tight` / `wont_fit` / `unknown`. Subject is
  `hf:Owner/Model`.

### Changed

- Settings UI: `bool | None` fields absent from the submitted form now
  mean "no change" instead of being coerced to False. Fixes overlay
  pollution when adding nullable bool fields like `secret_guard`.

### Internal

- 908 → 971 tests (+63). Ruff clean. 0 mypy errors against 109 files.
- 3 new config blocks: `compose_lint`, `tag_lag`, `web.preflight_gate`.
- 3 new CLI dispatch paths (no new commands — extends `analyse` via
  the URL dispatcher).

## v0.4.2 — 2026-06-03

Five backlog items shipped together. Each one is a self-contained
module + tests; nothing forces the user to adopt them — every new
feature is opt-in via its own config block.

### Added — detectors

- **Backup-staleness adapter** (`backup_health.py`, `homelabsage
  backup-check`). Probes restic / borg / kopia for last-snapshot age
  and emits a finding per repo (`info` / `medium` / `critical`) that
  the auditor surfaces. Credentials never live in HomelabSage config —
  each repo's `env:` block is passed straight through to the subprocess,
  the same way the user's nightly cron already sources them. Severity
  ladder defaults `warn=2d` / `critical=7d` and is per-repo overridable.
- **Log-anomaly detector** (`log_anomaly.py`, `homelabsage
  log-anomaly`). Walks every running container, samples the last hour
  of logs, counts ERROR-class lines and compares the rate against a
  per-container rolling baseline kept in `log_samples`. Fires a
  `log_anomalies` row when the latest sample is ≥`sigma_threshold`
  standard deviations above the baseline mean — but only after the
  baseline has ≥`min_samples` samples, so freshly-added containers
  spend a warm-up period instead of firing false positives. Disabled by
  default.
- **Post-update health check** (`health_check.py`, `homelabsage
  health-check`). When the user marks an update APPLIED, a probe is
  queued N minutes out. The probe scans recent logs for a small fixed
  list of bad signals — silent CPU fallback from CUDA/ROCm, OOM kills,
  panic backtraces, permission-denied — and writes one
  `health_checks` row per probe. Catches the regressions release notes
  don't mention (a workload that used to be fast quietly running on
  CPU).

### Added — UX / surfaces

- **News article / changelog URL analyser** — sub-case (c) of the
  URL analyser roadmap item. `homelabsage analyse https://blog/post`
  now fetches the page, extracts the main content with `trafilatura`
  (falling back to a naive HTML strip when the library isn't
  installed), and runs the analyzer prompt against it. Subject becomes
  `article:<host>` so it's distinct from container subjects in the
  dashboard. The CLI dispatcher route is unchanged — the user pastes
  any URL and the right path takes over.
- **Spanish UI strings** (`i18n.py`, `i18n.lang` config). Minimal
  catalog: nav + dashboard headings + status verbs + wizard labels.
  English remains the source of truth; missing Spanish keys fall back
  silently so a new template never crashes. Test ensures every English
  key has a Spanish translation to catch drift early.

### Internal

- 844 → 908 tests (+64). Ruff clean, 0 mypy errors against 106 source files.
- Four new DB tables: `health_checks`, `health_check_queue`,
  `log_samples`, `log_anomalies`. Forward-only `CREATE IF NOT EXISTS`.
- New mixins: `HealthCheckMixin`, `LogAnomalyMixin`.
- Four new config blocks: `backup_health`, `health_check`,
  `log_anomaly`, `i18n` — all default-off / default-English so existing
  YAML keeps working unchanged.
- New CLI commands: `backup-check`, `health-check`, `log-anomaly`.

## v0.4.1 — 2026-06-02

Three high-value items promoted from the v0.5/v0.6 backlog now that the
detectors they need are all shipping data.

### Added

- **Proactive auditor** (`audit.py`, `/audit` + `/api/audit`,
  `homelabsage audit` CLI). Synthesises every detector — repo_health,
  alternatives, orphans, CVE counts, image-size growth, the
  pending_dispatches queue, parity-active state — into one prioritised
  Markdown report. Hard rule: every line cites the concrete source
  signal verbatim. Writes `notes/audit.md` so the curator picks it up.
- **Server chronicle** (`chronicle.py`, `homelabsage chronicle --days N`).
  Narrative timeline of homelab events in a lookback window:
  applied / dismissed / breaking / hold-recommended / severity_jump.
  Plain Markdown for `notes/chronicle.md`. No LLM in the loop — the
  analyzer already ran on each row.
- **Docker Hub URL analyser** — `homelabsage analyse <hub-url>`. Sub-case
  (b) of the URL analyser roadmap item lands; the CLI now dispatches
  on URL shape (GitHub / Codeberg / Docker Hub). Pulls Docker Hub repo
  metadata + latest-tag info + `find_alternatives` context.

### Internal

- 799 → 844 tests (+45). Ruff clean, 0 mypy errors.
- New nav link `/audit` in the web UI.

## v0.4.0 — 2026-06-02

The "Watchtower-was-archived" release — eight research-driven features that
sharpen the wedge HomelabSage has over the now-archived Watchtower
(github.com/containrrr/watchtower discussion #2135, 17 Dec 2025) and over
the notify-only WUD/Diun cohort. None of them reason about *what changed*
between versions; v0.4 leans into that gap.

### Added — analyzer

- **Release-notes diff summariser** (`releases_diff.py`,
  `sources.docker.releases_diff` on by default). Fetches every
  GitHub/Codeberg release strictly between the local tag and the
  candidate tag, concatenates the bodies, hands the diff to the LLM.
  New prompt rule mines the diff for breaking changes the user actually
  crosses by upgrading — not just the latest release body.
- **Tag-pattern intelligence** (`tag_patterns.py`). Infers each
  image's tag scheme (semver / calver / build_suffix / digest /
  floating) from the published tag listing so the analyzer can skip
  out-of-band candidate tags (e.g. an experimental `:cuda12` on a
  semver-dominant image).

### Added — UX / surfaces

- **`/mcp` Model Context Protocol endpoint** (`mcp.py`). JSON-RPC 2.0
  over HTTP exposing seven tools — `health`, `list_updates`,
  `get_update`, `set_update_status`, `list_diagnostics`,
  `list_watched_repos`, `list_pending_dispatches`. Lets the user's
  Claude Code / Cursor / ChatGPT desktop read homelab state directly.
- **Explain mode** (`/updates/<id>/explain`,
  `analysis_explainers` table). Stores the exact prompt sent + raw
  response received per analyzed update. Click any verdict for a
  side-by-side audit trail of prompt / response / notes consulted.
- **Homepage + Homarr widget endpoints** (`/widget/homepage`,
  `/widget/homarr`). Auth-bypassed read-only JSON for dashboard
  cards — pending count, severity buckets, parity_active,
  queued_dispatches, last_scan_at.
- **`/usage` page** (`db/usage.py`). Per-LLM-call rows + 30-day
  rolling per-provider/model totals. Helps users see "where did my
  Groq free-tier quota go?". Tokens come from provider responses
  when available; chars/4 estimate otherwise.

### Added — notifications

- **Severity-aware batching** (`outputs/batch.py`,
  `outputs.batching` config block). When ≥`min_count` updates below
  `below_severity` fire in one scan, push channels receive a single
  rollup at end-of-scan instead of one ping per item. Critical/High
  keep firing immediately.

### Added — migration aid

- **Watchtower migration helper**
  (`homelabsage watchtower-migrate`, `watchtower_migrate.py`).
  Detects a running `containrrr/watchtower` or `nicholas-fedor/watchtower`
  container, parses its env + CLI args + per-container labels, and
  prints a Markdown report recommending what to include / skip /
  monitor-only when migrating each container to HomelabSage.

### Changed

- **`LLMClient` instrumented** for token-usage. New `LastCall`
  dataclass replaces the previous `(prompt, raw_response)` tuple;
  it now also carries `tokens_in`, `tokens_out`, `tokens_estimated`,
  `duration_ms`, `succeeded`. The Engine writes a usage row + an
  explainer row per call. `_call()` is kept as a thin wrapper for
  back-compat — `_call_with_usage()` is the new canonical method.
- Web `/widget/*` paths bypass HTTP Basic Auth so dashboards can
  scrape without juggling credentials.

### Internal

- 700 → 799 tests (+99). Ruff clean, 0 mypy errors.
- Three new DB tables: `analysis_explainers`, `llm_usage`. Forward-
  only `CREATE IF NOT EXISTS` — no migration script needed.

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
