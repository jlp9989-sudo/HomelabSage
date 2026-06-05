# Changelog

All notable changes ship here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely. Dates are UTC.

## v0.8.4 — 2026-06-04

- **`dangling_images.py`** + **MCP `dangling_images` tool**. Pure
  walk over docker SDK image list, finds `<none>:<none>` orphans
  with total bytes/MiB. Graceful when docker isn't reachable
  (`{ok: false, error: ...}`) — agents on docker-less hosts don't
  crash.

### Internal

- 1525 → 1534 tests (+9), ruff clean, 0 mypy errors against 174 files.

## v0.8.3 — 2026-06-04

- **`GET /api/audit/categories`** — HTTP mirror of the MCP
  `audit_categories` tool. Compact histogram shape for Homepage
  stripe widgets.
- **MCP `is_snoozed(update_id)`** — quick lookup `{snoozed: bool,
  snooze_until: str|null}`. Past timestamps return `snoozed=False`
  (matches engine/flush semantics). Corrupt timestamps land in
  `error` rather than misreporting.

### Internal

- 1519 → 1525 tests (+6), ruff clean, 0 mypy errors against 173 files.

## v0.8.2 — 2026-06-04

Mute-list ergonomics + compact audit roll-up.

### Added

- **`homelabsage audit-mute purge-expired`** sub-verb +
  **`DELETE /api/audit/mutes/expired`** + MCP
  **`audit_mute_purge_expired`**. Drops rows whose `expires_at` is
  in the past. The build_report filter already ignores expired rows;
  this is housekeeping for long-running deployments.
- **`homelabsage audit-mute add-from-stdin`** sub-verb. Reads JSONL
  fingerprints from stdin and bulk-mutes each. Designed to pair
  with `audit --jsonl | jq -c 'select(...)'`. Malformed lines
  counted in a `skipped` tally; well-formed but field-missing
  lines also skipped (`category` / `source_kind` / `source_ref`
  required).
- **MCP `audit_categories` tool**. Compact `{counts_by_category,
  counts_by_severity, total, healthy}` payload — no findings list.
  Right shape for a dashboard widget that only needs the histogram.

### Internal

- 1509 → 1519 tests (+10), ruff clean, 0 mypy errors against 173 files.
- 2 new CLI sub-verbs, 1 new web route, 2 new MCP tools.
- Pre-existing v0.8.0 defensive test (`test_milestone_version_is_0_8_0`)
  relaxed to `version >= 0.8` so milestone-bump tests don't pin
  patch versions.

## v0.8.1 — 2026-06-04

Audit-finding mute list — full surface (db, filter, CLI, HTTP, MCP).

### Added

- **`audit_mutes` SQL table** + **`AuditMutesMixin`** on `Database`.
  Fingerprint = `(category, source_kind, source_ref)`. Optional
  `expires_at` (NULL = permanent). `PRIMARY KEY` makes re-adds
  idempotent.
- **`audit.build_report` filters muted findings** in-memory before
  severity sort, so `audit.md`, `audit_history.jsonl`, `/audit`,
  `/api/audit`, MCP `audit_diff`, and the SSE stream all observe
  the mute. No retro-active filtering of already-persisted history.
- **`homelabsage audit-mute add/list/remove` CLI** (sub-Typer
  group). `add` accepts `--for 7d|12h|30m` relative, `--until ISO`
  absolute, or neither (permanent). `--include-expired` on list.
- **`/api/audit/mutes`** (GET/POST/DELETE). POST validates ISO 8601
  + required fields; DELETE returns 404 when no fingerprint matches.
- **MCP `audit_mute_add` / `audit_mute_list` / `audit_mute_remove`**.

### Internal

- 1493 → 1509 tests (+16), ruff clean, 0 mypy errors against 173 files.
- 1 new DB table + mixin, 1 new CLI sub-Typer (3 verbs), 3 new web
  routes, 3 new MCP tools.

## v0.8.0 — 2026-06-04 — milestone

Consolidates everything since v0.7.0. Single new CLI
(`homelabsage features`) to round out the version surface — the
rest is the milestone bump itself.

### Added

- **`homelabsage features` CLI**. Prints version + parsed
  `version_parts` + the same feature flag map exposed by
  `GET /api/version` and the MCP `version` tool. `--json` emits
  one JSON object on stdout for cron / ops integration.

### Summary of the v0.7.x line

The v0.7 series shipped:

| Version | Highlight                                                  |
| ------- | ---------------------------------------------------------- |
| v0.7.0  | `homelabsage doctor` bundled diagnostic                    |
| v0.7.1  | `/api/doctor` + MCP `doctor` + review-driven safety fixes  |
| v0.7.2  | `homelabsage snooze` CLI + list-snoozed + audit `--jsonl`  |
| v0.7.3  | `audit_history.jsonl` persistence + `/api/audit/diff` + `homelabsage init` |
| v0.7.4  | `/api/audit/history` + audit-alert webhook + `doctor --watch` |
| v0.7.5  | MCP `audit_history`/`audit_diff` + `compose-graph` CLI + `audit --severity` |
| v0.7.6  | `audit-prune` + bulk snooze clear + `doctor --severity-floor` |
| v0.7.7  | `audit --diff-only` + MCP `audit_prune` + raw JSONL stream |
| v0.7.8  | `homelabsage status` + `/api/version` + MCP `clear_pending_dispatches` |
| v0.7.9  | `purge_old_updates` + `scan_window_check` + `version_parts`|
| v0.8.0  | `homelabsage features` CLI                                 |

### Internal

- 1489 → 1493 tests (+4), ruff clean, 0 mypy errors against 171 files.
- 1 new CLI subcommand.

## v0.7.9 — 2026-06-04

DB retention, scan-window introspection, version_parts for agents.

### Added

- **`db.purge_old_updates(older_than_days, statuses, dry_run)`** +
  **`homelabsage purge --days N --dry-run`** + MCP
  **`purge_old_updates`**. Drops APPLIED + DISMISSED rows older
  than N days. Snoozed rows are never purged (the snooze is a
  remind-me signal we honour here too). Single-statement SELECT +
  DELETE so no race vs concurrent inserts.
- **MCP `scan_window_check`**. Returns `{enabled, blocked, reason}`
  so agents can decide between "trigger /run now" vs "wait until
  the window clears" without parsing config themselves.
- **`version_parts`** in the existing MCP `version` tool (and
  HTTP equivalent kept symmetric). Returns `[major, minor, patch]`
  ints so an agent can write `version_parts >= [0,7,0]` instead
  of parsing strings.

### Internal

- 1479 → 1489 tests (+10), ruff clean, 0 mypy errors against 170 files.

## v0.7.8 — 2026-06-04

Status command, pending-dispatch bulk clear, version surface.

### Added

- **`homelabsage status` CLI**. One-line-per-section summary
  (Updates by status, Audit by severity, Parity gate, Pending
  dispatches, Snoozed updates) for a terminal-bar / status-strip
  glance. Always exits 0 — use `doctor` for the exit-code form.
- **`db.clear_pending_dispatches()`** + **MCP
  `clear_pending_dispatches`**. Operator panic-button to drop the
  whole push-dispatch queue after a misconfigured output queued
  thousands of items.
- **`GET /api/version`** (auth-bypassed) + **MCP `version`**.
  Returns `{version, features: {doctor, audit_history, snooze,
  audit_alerts}}`. Lets a downstream agent / dashboard widget gate
  behaviour on min-version requirements.

### Internal

- 1472 → 1479 tests (+7), ruff clean, 0 mypy errors against 169 files.

## v0.7.7 — 2026-06-04

Three audit-surface extensions.

### Added

- **`homelabsage audit --diff-only`**. Show only findings that are
  NEW vs the latest persisted snapshot. Composes with `--jsonl`
  and `--severity`. Pure read; never appends history, never writes
  notes (no race with concurrent `audit` runs).
- **MCP `audit_prune` tool**. `keep_last` int param (default 100).
  Returns `{ok, dropped, keep_last}`.
- **`GET /api/audit/history.jsonl`**. Raw NDJSON stream of
  `audit_history.jsonl` for backup / offline analysis. 64 KB
  chunked read, bounded RAM. `Content-Disposition: attachment` so
  browsers prompt a save dialog.

### Internal

- 1464 → 1472 tests (+8), ruff clean, 0 mypy errors against 168 files.

## v0.7.6 — 2026-06-04

5-item batch: audit-history pruning, MCP compose-graph + bulk snooze
clear, HTTP recurring-failures, doctor severity floor.

### Added

- **`audit_history.prune(keep_last)`** + **`homelabsage audit-prune
  --keep N`**. Atomic truncate (tmp file + rename) so a kill
  mid-write never leaves a half-truncated history. Default keeps
  100 newest rows.
- **MCP `compose_graph_mermaid` tool**. Returns `{mermaid,
  service_count, edge_count}`. `paths` param overrides config.
  Agents can read the graph without invoking the CLI.
- **`db.clear_all_snoozes()`** + **`DELETE /api/updates/snoozed`** +
  **MCP `clear_all_snoozes`**. Bulk-clear every snooze in one call;
  returns the count.
- **`GET /api/updates/recurring-failures?min_count=&limit=`**. HTTP
  mirror of the existing MCP `recurring_failures` tool so Homepage
  / Homarr widgets can display a "stop bashing this wall" list.
- **`homelabsage doctor --severity-floor {info|medium|high|critical}`**.
  Audit findings below the floor don't flip the exit code. Probe
  failures (TLS/DNS/disk/compose) still flip it regardless — the
  flag is intentionally conservative.

### Internal

- 1449 → 1464 tests (+15), ruff clean, 0 mypy errors against 168 files.
- 1 new module (`cli/audit_prune.py`), 2 new MCP tools, 2 new web
  routes (DELETE snoozed + GET recurring-failures), 1 new db helper.

## v0.7.5 — 2026-06-04

MCP exposure of audit history + diff, compose graph mermaid export,
and audit severity filter.

### Added

- **MCP `audit_history` + `audit_diff` tools**. Agents can now read
  the audit history pagination + current diff vs latest snapshot
  without the HTTP route. `audit_history` returns the compact
  summary shape (counts only, never the full findings list).
- **`homelabsage compose-graph` CLI**. Renders the compose
  dependency graph as a Mermaid `flowchart LR`. Services grouped by
  project subgraphs, `A --> B` edges where A depends_on B. `--out`
  writes a Markdown-fenced file ready for GitHub / Notion /
  Homepage widgets.
- **`homelabsage audit --severity {info|medium|high|critical}`**.
  Hides findings below the floor in both Markdown and `--jsonl`
  output. Filtering is presentation-only: `audit_history.jsonl` +
  `audit.md` written by `run_audit` stay full-fidelity.

### Internal

- 1434 → 1449 tests (+15), ruff clean, 0 mypy errors against 167 files.
- 2 new MCP tools, 1 new CLI subcommand, 1 audit CLI flag.

## v0.7.4 — 2026-06-04

Audit-history surface extended: paginated history endpoint, webhook
alerts on new findings, and continuous `doctor --watch` mode.

### Added

- **`GET /api/audit/history?limit=&offset=`** — paginated list of
  past audit snapshots (newest first). Returns the COMPACT shape
  (counts + finding_count, never the full findings list) so a
  dashboard polling history of a busy homelab doesn't pull MBs.
- **`audit_alerts` config** (`AuditAlertsConfig`, default off).
  When enabled + `webhook_urls` populated, `run_audit()` POSTs a
  rollup payload `{type, generated_at, new, resolved,
  counts_by_severity}` once per scan when a NEW finding meets the
  severity floor. Per-URL failures log + continue. Resolved-only
  scans don't fire — resolution is not an alert.
- **`homelabsage doctor --watch N`**. Continuous diagnostic mode
  that re-runs every N seconds (minimum 5). Highlights verdict
  changes between iterations. Ctrl-C exits cleanly.

### Internal

- 1420 → 1434 tests (+14), ruff clean, 0 mypy errors against 166 files.
- 1 new module (`audit_alert.py`), 1 new config block, 1 new web
  route, 1 new history helper (`list_history`).

## v0.7.3 — 2026-06-04

Audit-history persistence + `/api/audit/diff` + `homelabsage init`.

### Added

- **`audit_history.py`** — append-only JSONL log at
  `<notes_dir>/audit_history.jsonl`. `run_audit()` writes one row
  per scan. Diff helper computes `(new, resolved)` based on a
  fingerprint of `(category, source_kind, source_ref)` — stable
  across runs even when severity/title rephrase.
- **`GET /api/audit/diff`** — returns
  `{previous_snapshot, new, resolved}` against the latest persisted
  snapshot. Lets a dashboard show "what changed since last scan?"
  without polling the full report.
- **`homelabsage init` CLI**. Interactive bootstrap of a minimal
  `config.yaml` with sane defaults. `--non-interactive` for CI,
  `--force` to overwrite, refuses to clobber an existing file
  without explicit consent.

### Internal

- 1407 → 1420 tests (+13), ruff clean, 0 mypy errors against 165 files.
- 1 new module, 1 new CLI subcommand, 1 new web route, 1 audit
  side-effect (history append in `run_audit`).

## v0.7.2 — 2026-06-04

Snooze surface tied off + audit JSONL for shell pipelines.

### Added

- **`GET /api/updates/snoozed`** + **`db.list_snoozed()`**. Returns
  updates whose `snooze_until` is in the future, sorted soonest-to-
  expire first. Lexicographic comparison on the ISO 8601 strings —
  valid given the snooze setter normalises to UTC with `+00:00`/`Z`.
- **`homelabsage snooze` CLI**. Operations:
  - `snooze <id> --until <iso>` set absolute timestamp
  - `snooze <id> --for <7d|12h|30m>` relative shorthand
  - `snooze <id> --clear` clear
  - `snooze --list` see currently-snoozed
  Rejects passing both `--until` and `--for`.
- **MCP `list_snoozed` tool**. Same payload shape as the HTTP endpoint.
- **`homelabsage audit --jsonl`**. Streams one JSON object per
  finding to stdout — pipeable into `jq` for filtering. Bypasses
  the `notes/audit.md` write so the JSONL form stays side-effect-
  free.

### Internal

- 1396 → 1407 tests (+11), ruff clean, 0 mypy errors against 163 files.
- 1 new CLI subcommand, 1 new web route, 1 new MCP tool, 1 new
  db helper.

## v0.7.1 — 2026-06-04

Doctor surface (HTTP + MCP) + review-driven fixes from a background
code-review agent.

### Added

- **`homelabsage.doctor` module**. Shared probe orchestrator —
  reused by the CLI, `GET /api/doctor`, and the new MCP `doctor`
  tool. Structured per-section report: `ok` / `skipped` / `findings`.
- **`GET /api/doctor`** — JSON mirror of the doctor CLI. Auth-bypassed
  (in `AUTH_BYPASS_EXACT`) like `/api/stack-health` so HA / Homepage
  widgets / Kuma can scrape. `?skip_llm=1` for offline runs.
- **MCP `doctor` tool**. Returns the same structured report so agents
  can ask "is the homelab healthy?" in one call.

### Fixed (from review)

- **`_flush_pending_dispatches` now honours snooze**. A queued push
  for a since-snoozed update is held in the queue (not deleted)
  rather than firing when the parity gate clears. Regression test
  added.
- **GitHub webhook security tightened**. `/api/webhook/github-release`
  is now in `AUTH_BYPASS_EXACT` (so GitHub deliveries don't 401 when
  Basic Auth is enabled) AND the HMAC secret is MANDATORY (endpoint
  returns 503 when `GITHUB_RELEASE_WEBHOOK_SECRET` is unset).
  Previously the secret was optional → bypassed endpoint accepted
  anonymous DB writes.
- **`set_status` idempotent on FAILED→FAILED**. `failure_count` only
  bumps on a true transition into FAILED; repeated bulk / UI clicks
  no longer inflate the recurring-failure audit signal. SQL `CASE`
  expression instead of two separate UPDATEs.

### Internal

- 1389 → 1396 tests (+7), ruff clean, 0 mypy errors against 162 files.

## v0.7.0 — 2026-06-04 — milestone

Consolidates everything since v0.5.0. New `homelabsage doctor`
one-shot diagnostic ties every v0.6.x probe (TLS, DNS, disk,
LLM-health, audit, compose-override, env-perms) into a single
exit-coded command suitable for cron / Kuma push.

### Added

- **`homelabsage doctor` CLI** (`cli/doctor.py`). Runs every active
  probe and prints a coloured summary. Exit codes:
  `0` healthy, `1` ≥1 actionable finding, `2` LLM unreachable.
  `--skip-llm` for offline runs. The 30-second answer to "is my
  homelab healthy right now?".

### Summary of the v0.6.x line

The v0.6 series added (in order of shipping):

| Version | Highlight                                                    |
| ------- | ------------------------------------------------------------ |
| v0.6.1  | Search endpoint + webhook receiver `/api/inbox/<source>`     |
| v0.6.2  | Restart-flapping + exposed-port + Slack + API-key auth       |
| v0.6.3  | Star/bookmark + Pushover + MCP explain + Notion archive CLI  |
| v0.6.4  | TLS probe + MS Teams + volume orphans + stack export CLI     |
| v0.6.5  | Healthcheck-stale + disk-pressure + compose-override + scan-window |
| v0.6.6  | MCP audit tools + env-diff + LLM health gate + audit SSE     |
| v0.6.7  | Restart-policy + env-perms + recurring-failures + env-diff CLI |
| v0.6.8  | Snooze + container-disappearance + DNS probe + 3 MCP         |
| v0.6.9  | Snooze engine wiring + GitHub release webhook + restart-drift |
| v0.7.0  | `homelabsage doctor` bundled diagnostic                      |

### Internal

- 1386 → 1389 tests (+3), ruff clean, 0 mypy errors against 161 files.
- 1 new CLI subcommand.

## v0.6.9 — 2026-06-04

Snooze enforced in the engine, GitHub release webhook receiver, and
a restart-drift detector.

### Added

- **Snooze honoured by the engine**. `Engine._snooze_active_for()`
  parses the `snooze_until` timestamp; push outputs (Telegram /
  Discord / Slack / etc.) skip dispatch when the snooze is still in
  the future. Persistent outputs (Notion) still write so the
  dashboard stays consistent. Corrupt timestamps log + return
  None — they never silence an update permanently.
- **GitHub release webhook** — `POST /api/webhook/github-release`.
  Accepts the standard GitHub release-event payload, validates with
  optional `GITHUB_RELEASE_WEBHOOK_SECRET` via `X-Hub-Signature-256`
  HMAC, only processes `action: released` (skips draft / pre-release
  / edit). 1 MB body cap. Subject = `repository.full_name`,
  version = `release.tag_name`, body = `release.body` truncated to
  64 KB.
- **Container last-started drift detector** (`restart_drift.py`).
  Pure function; takes `started_at` + `now` + optional
  `last_applied_at`. Flags `info`-level when the restart happened
  within `[min_hours, max_hours]` AND no APPLIED update lines up
  within `apply_window_hours`. Useful for noticing "mealie restarted
  at 08:53 — what happened?" without the noise of older restarts.

### Internal

- 1370 → 1386 tests (+16), ruff clean, 0 mypy errors against 160 files.
- 1 new module, 1 new web route, 1 new engine method.

## v0.6.8 — 2026-06-04

Five new surfaces: snooze, container-disappearance tracking, DNS
probe, plus three MCP tools that expose snooze + recurring failures
+ DNS.

### Added

- **Per-update snooze**. New `snooze_until` column on `updates`
  (ISO 8601 UTC). New `POST /api/updates/<id>/snooze` accepts
  `{"snooze_until": ...}`. Body validation parses the timestamp.
  Future scope: have outputs honour the snooze automatically (the
  DB column is in place; engine wiring deferred).
- **Container-disappearance tracker** (`disappearance.py`). Pure
  `diff_snapshots(prev, current_names)` + atomic
  `save_snapshot` / `load_snapshot` JSON helpers. Lets the auditor
  flag "container X was here last scan, isn't now" without a new
  DB table — snapshot is single-row state.
- **DNS resolution probe** (`dns_check.py`). Stdlib
  `socket.getaddrinfo` with bounded timeout (saved/restored via
  context manager). Returns one `DNSFinding` per NXDOMAIN /
  resolution failure. Companion to `tls_check`: catches drift in
  the underlying DNS record even when the cert is fresh.
- **3 MCP tools** — `snooze_update`, `recurring_failures`,
  `dns_check`. The DNS tool defaults its hostname list to the hosts
  derived from `cfg.tls_check.urls` so the user doesn't have to
  configure two lists.

### Internal

- 1348 → 1370 tests (+22), ruff clean, 0 mypy errors against 159 files.
- 3 new modules, 1 new DB column, 1 new web route, 3 new MCP tools,
  2 new db helpers (`set_snooze`, `get_snooze`).

## v0.6.7 — 2026-06-04

Four orthogonal additions across container metadata, filesystem
security, retry telemetry, and operator tooling.

### Added

- **Restart-policy auditor** (`restart_policy.py`,
  `sources.docker.detect_restart_policy = true`). Reads
  `HostConfig.RestartPolicy.Name`; flags `""` / `"no"` as `medium`
  because a container with no restart policy disappears on next
  reboot. Strict mode (`detect_restart_policy_strict`) also flags
  bounded `on-failure` (off by default).
- **`.env` permissions auditor** (`env_perms.py`). Walks the
  compose scan paths one level deep, stats `.env` and `*.env`
  files. World-readable → `medium`, group-writable → `high`,
  world-writable → `critical`. Never reads file contents (that's
  `secret_guard`'s job). Surfaces as `env_perms` auditor finding.
- **Recurring-failure tracker**. New `failure_count` column on the
  `updates` table, auto-incremented every time the user flips
  `status` to FAILED. New `db.list_recurring_failures(min_count=2)`
  + auditor finding (`recurring_failure` category, severity scales
  with count). Lets the user notice "I keep retrying this and it
  keeps breaking — time to pin or dismiss".
- **`homelabsage env-diff <container>` CLI**. Wraps `env_diff.py`:
  reads the container's effective env via docker SDK, fetches the
  image's declared `Config.Env`, prints NEW/REMOVED/CHANGED with
  severity colouring. Exits 1 when any finding is ≥ medium so it
  can drive cron / CI alerts.

### Internal

- 1330 → 1348 tests (+18), ruff clean, 0 mypy errors against 157 files.
- 3 new modules, 1 new CLI subcommand, 1 new DB column, 2 new
  detectors wired into the docker plugin, 2 new auditor categories.

## v0.6.6 — 2026-06-04

Three new MCP tools exposing v0.6.4/v0.6.5 detectors, plus an env-diff
detector, a pre-scan LLM health gate, and an audit SSE stream.

### Added

- **3 MCP tools** — `disk_pressure_check`, `compose_overrides`,
  `tls_check_run`. Each accepts an optional `paths` / `urls`
  param-override; defaults to the configured list. Agents can now
  trigger the v0.6.4–v0.6.5 detectors directly without the
  auditor pass.
- **Container env-var diff** (`env_diff.py`). Pure function over
  `(container_env, image_env)` returning NEW / REMOVED / CHANGED
  rows. NEW with credential-like suffix (`_TOKEN`, `_KEY`,
  `_PASSWORD`, etc.) escalates to `high`; plain NEW is `medium`;
  REMOVED + CHANGED are `info`. Filters PATH/HOME/HOSTNAME noise.
- **LLM-backend health gate** (`llm_health.py` +
  `LLMHealthGateConfig`, default off). Pre-scan GET probe of the
  active LLM endpoint (`/v1/models` then `/health` / `/healthz`).
  401/403 are treated as alive (auth is the user's problem). On
  failure the scan is skipped and counted as `skipped` so the
  schedule still shows activity. 3s default timeout.
- **`GET /api/audit/stream`** — Server-Sent Events stream of audit
  findings. One `event: finding` per row plus a final
  `event: done` carrying the summary. Disables nginx proxy
  buffering. Lets a UI render findings incrementally without
  waiting for the full JSON blob.

### Internal

- 1310 → 1330 tests (+20), ruff clean, 0 mypy errors against 154 files.
- 3 new modules, 1 new config block (`LLMHealthGateConfig`), 3 new
  MCP tools, 1 new web route.

## v0.6.5 — 2026-06-04

Four orthogonal signals: a missing-from-restart_freq health gap, a
disk-pressure auditor, a compose-override detector, and a scheduler
quiet window distinct from the push-output one.

### Added

- **Healthcheck-staleness detector** (`healthcheck_stale.py`,
  `sources.docker.detect_healthcheck_stale = true`). Reads
  `State.Health` from `docker inspect`: catches `restart: always`
  containers that are RUNNING but `unhealthy` for hours — a gap
  `restart_freq` doesn't cover. Severity by duration: ≥72h critical,
  ≥24h high, ≥4h medium. Stale-since timestamp derived from the
  oldest failing log entry when every retained entry failed.
- **Disk-pressure auditor** (`disk_pressure.py` +
  `DiskPressureConfig`, default off). Pure `shutil.disk_usage` over
  user-listed paths. Two-axis severity (% AND absolute) so a 4 TB
  pool with 50 GB free flags even at 1.2% utilisation. Same-device
  dedupe via `st_dev` so `/mnt/user` and `/mnt/cache` don't
  double-report. Plugs into the auditor as a `disk_pressure` finding.
- **Compose-override detector** (`compose_override.py`,
  `sources.docker.detect_compose_override = true`). Scans
  `compose_scan_paths` for `docker-compose.override.yml` (and modern
  variants) siblings of the base file; surfaces as an `info`-severity
  auditor finding so the user knows the merged runtime graph
  differs from what cascade / linter analysed.
- **Scan-window scheduler** (`scan_window.py` + `ScanWindowConfig`,
  default off). Skips the entire scan during a `HH:MM-HH:MM` window
  — distinct from `QuietHoursMixin` which queues push notifications.
  Useful when the LLM backend is shared with a workstation that
  sleeps, or to respect ISP off-peak hours. Reuses
  `quiet_hours.parse_window` + `time_in_window` so wrap-around
  semantics are identical.

### Internal

- 1284 → 1310 tests (+26), ruff clean, 0 mypy errors against 152 files.
- 4 new modules, 4 new config blocks
  (`DiskPressureConfig`, `ScanWindowConfig`, plus 2 toggles on
  `DockerSourceConfig`), 2 new auditor finding categories.

## v0.6.4 — 2026-06-04

Four detectors-and-outputs additions, all decoupled from existing flows.

### Added

- **TLS cert expiry probe** (`tls_check.py` + `homelabsage tls-check`
  CLI). Stdlib `ssl`+`socket` — no `cryptography` dep. Reads peer
  cert `notAfter`, buckets days-until-expiry into severity
  (`critical` ≤0, `high` ≤warn/3, `medium` ≤warn, `info` otherwise).
  Best-effort: connection refused / DNS / handshake failures all
  return `ok=False` with a high-severity reason instead of raising.
- **Microsoft Teams output** (`outputs/msteams.py`,
  `MSTeamsOutputConfig`). Adaptive Card 1.5 wrapped in the
  `attachments[]` envelope Power Automate workflow webhooks expect.
  Severity → accent color (critical→attention, high→warning,
  medium→accent, info→good). Release-notes URL becomes a card action.
  The legacy MessageCard format is being deprecated through 2026 —
  we skip it entirely.
- **Docker volume orphan detector** (`volume_orphans.py`). Pure
  function on `(volumes, containers)`: walks every container's
  `Mounts[*]` filtering `Type=="volume"`, returns the volumes whose
  `Name` no container references. Caller is expected to pass
  `containers.list(all=True)` so stopped containers still count as
  users. Carries the `com.docker.compose.project` label so the user
  can see which stack a stranded volume belonged to.
- **`homelabsage stack <container>` CLI**. Single-command export of a
  container's compose graph + last N updates + rollback recipe,
  with `Sanitiser` redaction for IPs / hostnames / credentials. Drop
  into a forum post or bug report without manual scrubbing.

### Internal

- 1265 → 1284 tests (+19), ruff clean, 0 mypy errors against 148 files.
- 4 new modules, 1 new output, 2 new CLI subcommands, 2 new config
  blocks (`TLSCheckConfig`, `MSTeamsOutputConfig`).

## v0.6.3 — 2026-06-04

Four cleanup-and-quality-of-life items.

### Added

- **Star / bookmark updates** (new DB column `starred`, `POST/GET
  /api/updates/<id>/star`, `GET /api/updates/starred`,
  MCP `set_star` + `list_starred`). Lets the user flag updates for
  follow-up — survives status changes, exported in CSV.
- **Pushover output** (`outputs/pushover.py`, `outputs.pushover`).
  Dedicated Pushover (separate from apprise) with per-severity
  priority mapping. Emergency-mode (priority=2, retry-until-ack)
  is opt-in via `emergency_at_critical=true`.
- **MCP `explain` tool**. Returns the stored prompt + raw LLM response
  for an update — same data the `/updates/<id>/explain` page renders.
  Closes the last "agents can't audit AI verdicts" gap.
- **`homelabsage notion-archive --days N`** CLI. Bulk-archives Notion
  pages whose update is older than N days so a long-running install
  doesn't accumulate years of "applied" rows in the Notion DB.
  `--dry-run` for safety.

### Internal

- 1248 → 1265 tests (+17), ruff clean, 0 mypy errors against 143 files.
- 1 new DB column (`starred`), 1 new output (Pushover), 3 new MCP
  tools, 1 new CLI subcommand.

## v0.6.2 — 2026-06-03

Four new surfaces + a review-driven security pass. The review surfaced
3 critical bugs from v0.6.1 (stored-XSS, DoS, MCP-loop deadlock); all
fixed here with regression tests.

### Added

- **Restart-frequency detector** (`restart_freq.py`,
  `sources.docker.detect_restart_flapping` default on). Reads
  `c.attrs.RestartCount` + `State.StartedAt` and emits a finding
  when restart rate crosses 0.25/hr (medium) / 1/hr (high) / 60/hr
  (critical). Catches the "this container is crashlooping but I
  haven't noticed" case before the user pushes an update.
- **Exposed-port detector** (`exposed_ports.py`,
  `sources.docker.detect_exposed_ports` default on). Flags two
  patterns: (a) **privileged port** (<1024) bound to `0.0.0.0` /
  `::`; (b) any TCP/UDP port bound to a public (non-RFC1918,
  non-loopback) IPv4. `0.0.0.0:8080` (the homelab norm) does NOT
  flag. Attached as `Update.context.exposed_ports`.
- **Slack output** (`outputs/slack.py`, `outputs.slack`). Dedicated
  Slack incoming-webhook with Block Kit message — separate from
  apprise so the user gets the real Slack UI: header block + section
  blocks + context block + severity-coloured attachment stripe.
- **API-key auth** (`web.auth.api_keys`). Bearer-token alternative to
  Basic Auth — additive list of shared secrets the user enables for
  headless agents. Mixes with the existing `username/password` Basic
  Auth; either form grants access.

### Fixed (review-driven CRITICAL bugs from v0.6.0/v0.6.1)

- **MCP `analyze_url` + `csi` crashed inside the event loop**: they
  called `asyncio.run()` from inside the FastAPI async dispatcher,
  which raises `cannot be called from a running event loop`. New
  `_run_coro()` helper detects a running loop and schedules the
  coroutine on a worker thread with its own loop.
- **`/api/inbox` stored-XSS via `release_url`**: the receiver accepted
  `javascript:alert(1)` URLs which rendered in the dashboard. Now
  rejects anything not http(s)://, caps URL at 2048 chars.
- **`/api/inbox` DoS via huge body**: previously no cap, so a 100 MB
  POST hit the database. Now: 1 MB body cap (Content-Length pre-
  check), 64 KB release_notes truncation, 200/100-char caps on
  subject and version fields.
- **`/api/updates/search` and `POST /api/updates/bulk` blocked the
  event loop**: SQLite LIKE on 2000 rows + N synchronous writes
  inside async handlers. Now offloaded via `asyncio.to_thread`.
- **Webhook output header-injection**: user-config headers with
  `\r` / `\n` in keys/values now logged + dropped (no Auth header
  override) rather than passed through to httpx.
- **Auth-bypass typo risk**: paths now live in `AUTH_BYPASS_EXACT`
  (frozenset) + `AUTH_BYPASS_PREFIX` (tuple) — single source of
  truth, regression test asserts the exact set.

### Internal

- 1220 → 1248 tests (+28), ruff clean, 0 mypy errors against 141 files.
- 2 new detectors + 1 new output + 1 new auth scheme.
- 6 critical/important review findings fixed with regression tests in
  `tests/test_v062.py`.

## v0.6.1 — 2026-06-03

Three connection-layer features. The dashboard becomes searchable,
external systems can push events in, and the MCP surface picks up
search + user-note tools.

### Added

- **Update search** (`GET /api/updates/search?q=`, `db.search()`).
  Case-insensitive substring match over subject + summary +
  breaking_changes JSON + user_note. Capped at `limit=200`; empty
  query returns `{count: 0, items: []}` so the UI doesn't need a
  guard.
- **Webhook receiver** (`POST /api/inbox/<source>`). External systems
  POST `{subject, new_version, current_version?, release_url?,
  release_notes?, context?}` and HomelabSage creates an Update with
  `source=<source>`. Source slug restricted to `[a-z0-9_-]{1,32}`.
  Lets a Renovate/Dependabot/Watchtower webhook land in the same
  pipeline as the docker scanner.
- **MCP `get_user_note` / `set_user_note` / `search_updates`** tools.
  Agents can now read+write the free-text note column and search the
  history without falling back to raw JSON-RPC tools.

### Internal

- 1203 → 1220 tests (+17), ruff clean, 0 mypy errors against 138 files.
- Fixed a mypy false positive: `UpdatesMixin.list()` shadows the
  builtin in type annotations, so `search()`'s return is now spelled
  `builtins.list[AnalyzedUpdate]`.

## v0.6.0 — 2026-06-03

Observability + extensibility milestone. Three additive surfaces — the
project is now ready for Prometheus monitoring, third-party automation,
and inline user annotations.

### Added

- **Generic webhook output** (`outputs/webhook.py`, `outputs.webhook`).
  Bring-your-own-receiver — POSTs every update as a stable JSON
  envelope. Optional bearer token + extra headers. Use for Zapier,
  n8n, IFTTT webhooks, or your own FastAPI endpoint.
- **Prometheus `/metrics`** (`web/routes_metrics.py`). No 3rd-party
  dep — emits the OpenMetrics text format directly. Gauges for
  updates-by-status / updates-by-severity / pending-dispatches /
  interview-questions; counters for 24h heartbeats + 30d LLM
  token usage. Auth-bypassed so Prometheus scrapers work without
  credentials.
- **Per-update user notes** (DB column `user_note`, `POST/GET
  /api/updates/<id>/note`). Free-text annotation attached to any
  update. Forward-only ALTER at migrate time.

### Internal

- 1189 → 1203 tests (+14), ruff clean, 0 mypy errors against 138 files.
- 1 new output (Webhook), 1 new web route group (`/metrics`),
  1 new DB column + helper.

## v0.5.0 — 2026-06-03

Milestone release: every v0.4.x detector is now wired through the
engine + analyzer + auditor. The v0.4.x modules shipped capabilities;
v0.5.0 makes them all visible to the user without further config.

### Wired

- **Container age**: docker plugin attaches `Update.context.container_age`
  when `Created` is older than the new
  `sources.docker.container_age_warn_after_days` threshold (default
  180). Auditor severity bucket: <365d=info, <730d=medium, ≥730d=high.
- **Renovate config**: watched-repos plugin fetches `.renovaterc.json`
  on each Update and attaches the maintainer's auto-merge intent
  (`extends`, `automerge`, `automergeType`) on
  `Update.context.renovate`. New prompt rule treats this as a "the
  maintainer endorses this version range" signal.
- **PR-changelog**: watched-repos plugin synthesises a merged-PR
  list from `/compare/{base}...{head}` when the release body is
  thin (<500 chars). Surfaces as `Update.context.pr_changelog`.
  New prompt rule mines `merge_prs[*].subject` for breaking-change
  keywords the same way it does release notes.

### Internal

- 1184 → 1189 tests (+5 smoke tests for the wiring).
- 3 new prompt rules in `prompts/analyzer.md`.
- 1 new docker-source field (`container_age_warn_after_days`).
- 1 new auditor finding category (`container_age`).

## v0.4.9 — 2026-06-03

Six pure-data modules. The theme: ground every analyzer verdict in
more signals from upstream + reality, so the LLM hallucinates less.

### Added

- **Container age tracker** (`container_age.py`). Reads each
  container's `Created` timestamp and flags any older than 180 days.
  Multi-format timestamp parser truncates docker's 9-digit nanos to
  Python's 6-digit microseconds.
- **Compose validator** (`compose_validate.py`). Wraps
  `docker compose config -q` to catch SYNTAX errors the YAML loader
  misses (interpolation failures, `${VAR:?required}` violations).
  Pure subprocess; missing docker binary silently skips.
- **Retry queue with exponential backoff** (`retry_queue.py`). Pure
  scheduler: `should_retry_now(attempts, last_attempt_at, now)`
  returns whether the next retry window has opened. Schedule:
  `0/15min/1h/6h/24h` then give-up.
- **Stack-level grouped digest** (`stack_digest.py`). When ≥3
  services in the same compose project have updates, emit ONE
  rollup per project. Builds on the docker plugin's existing
  `compose_project` context field.
- **Renovate config reader** (`renovate_config.py`). Fetches
  `.renovaterc.json` / `renovate.json` from upstream repos and
  exposes `extends`, `automerge`, `automergeType`, package_rules
  count. Includes a forgiving JSON5 parser for comments + trailing
  commas the strict json module rejects.
- **PR-changelog summarizer** (`pr_changelog.py`). For repos with
  sparse release notes, walks `/compare/{base}...{head}` and
  extracts merged-PR titles. Matches both `(#1234)` squash form and
  `Merge pull request #N` classic form. Caps at 30 PRs.

### Internal

- 1150 → 1184 tests (+34), ruff clean, 0 mypy errors against 136 files.
- 6 new modules, all pure-data + best-effort.

## v0.4.8 — 2026-06-03

The big one. Eight features across notification, detection, observability
and agent surfaces. Total: 8 modules added, 32 new tests, all opt-in.

### Added — outputs

- **Apprise output** (`outputs/apprise.py`, `outputs.apprise`). Universal
  push wrapper via the `apprise` PyPI library — speaks 100+ services
  through a single URL syntax (Pushover, Mattermost, MS Teams, Slack,
  Mailgun, AWS SNS, …). The library is OPTIONAL; missing
  installation downgrades to a one-time WARN log line, output stays
  silent. Severity maps to apprise `notify_type` so services that
  visualise urgency colour the message correctly.
- **SMTP / email output** (`outputs/smtp.py`, `outputs.smtp`). Direct
  stdlib `smtplib`-based output. 587 → STARTTLS, 465 → implicit SSL,
  other ports → cleartext. Optional auth. One message per recipient.
  Subject carries severity for inbox filtering. Sync send runs in
  `asyncio.to_thread` so the engine's event loop never stalls on
  STARTTLS handshake.

### Added — detectors

- **Resource-limit absence detector** (`resource_limits.py`,
  `sources.docker.detect_resource_limits` on by default). Flags
  containers running without `mem_limit:` / `cpus:` — one OOMing
  container can take down the host. Treats default `CpuShares=1024`
  as "no real limit". Attaches `Update.context.resource_limits`.
- **Architecture-mismatch detector** (`arch_mismatch.py`). Maps the
  host's `platform.machine()` to docker's `os/arch` notation and
  checks the new image's multi-arch manifest for a matching entry.
  Variant suffixes (`arm64/v8`) are matched as covering the plain
  form. Surfaces as `Update.context.arch_mismatch`.

### Added — surfaces

- **Stack-health endpoint** (`GET /api/stack-health`). One-shot
  aggregator for dashboards: audit summary + update counts by status +
  parity state + pending-dispatches count + 24h heartbeat counts +
  backup-staleness verdicts + recent post-update failures. Schema is
  stable across responses (always-present keys with zero/empty
  defaults). Auth-bypassed like `/widget/*`.
- **CSV scan-diff** (`scan_diff.py`, `homelabsage scan-diff old.csv new.csv`).
  Compares two `homelabsage history` dumps and emits added / removed
  / status_changed / severity_changed sections in Markdown. Exits 1
  when any change.
- **MCP `analyze_url` + `csi` tools**. Agents can now invoke the URL
  analyser AND pull post-mortem CSI evidence (`llm: false` for
  air-gapped agents). The CLI/MCP gap closes — only `curate` stays
  CLI-only (side-effectful + slow).
- **`/profile` page + `GET /api/profile`**. Self-discoverable summary
  of every config block: which sources are enabled, which detectors
  are on, which outputs fire, severity floors, scheduler cron, i18n
  language, etc. Pairs with `/audit` — `/profile` answers "what
  could fire?", `/audit` answers "what did fire?".

### Internal

- 1118 → 1150 tests (+32), ruff clean, 0 mypy errors against 130 files.
- 2 new push outputs (Apprise, SMTP), 1 new web route group, 1 new CLI
  subcommand (`scan-diff`), 2 new MCP tools (`analyze_url`, `csi`).
- 2 new config blocks (`AppriseOutputConfig`, `SMTPOutputConfig`),
  2 new docker-source toggles (`detect_sidecars`, `detect_resource_limits`).
- mypy `overrides` extended with `apprise.*` and `trafilatura.*` —
  both are optional 3rd-party libs.

## v0.4.7 — 2026-06-03

Four features + review-driven polish on v0.4.4-v0.4.6. The review
flagged 3 critical bugs and 12 important findings; all critical and
most important are fixed here, with regression tests.

### Added

- **Watched-repo enrichment** (`watched_enrich.py`). For each
  `github_watched` Update, the plugin now also fetches `topics` +
  `homepage` + a README excerpt (≤3 KB, base64-decoded from the
  GitHub `/readme` endpoint, fallback for Codeberg/Gitea). Surfaces
  as `Update.context.watched_enrich`. Closes the parity gap with the
  docker plugin's `fetch_readme` path.
- **Sidecar discovery** (`sidecars.py`, `sources.docker.detect_sidecars`
  on by default). Walks every running container's
  `HostConfig.NetworkMode` / `PidMode` for `container:<name>` refs and
  emits `SidecarLink` entries on the PRIMARY's `Update.context.sidecars`.
  Compose-graph `init/setup/migrate`-suffixed services with exactly one
  dependent count too. The analyzer prompt mentions sidecars in the
  recommendation so the user knows "restarting qbittorrent will also
  drop the gluetun VPN sidecar".
- **Heartbeat history** (`db/heartbeats.py`, new `heartbeats` table).
  The engine's `_heartbeat_ok` now records EVERY ping (success or
  failure) with status_code + error + duration. `db.heartbeat_summary`
  gives `{succeeded, failed, last_ok_at, last_failure_at}` over a
  window — drop-in data for an Uptime-Kuma sanity widget.
- **Auto-apply whitelist** (`auto_apply.py` + `AutoApplyConfig`).
  Opt-in safe-list of subjects allowed to flip APPLIED automatically.
  Five blocks before fire: not in allow-list, no analysis, severity >
  ceiling, breaking_changes non-empty, pin_violation present. Records
  `stats["auto_applied"]` per scan. Default disabled, allow-list only
  (no globs), `max_severity="info"`.

### Fixed (review-driven)

- **MCP `rollback_recipe` returned only the CLI form** — `build_recipe`
  was called without the compose graph, so the compose-file half was
  always empty. Now builds the graph from `cfg.sources.docker.compose_scan_paths`.
- **`rollback.prior_image` was an invalid docker ref** on floating-tag
  containers (`slug@sha256:abc<full-digest-needed>` was bogus). Now
  the ref stays a valid `<slug>:<previous-digest-needed>` placeholder,
  and the 12-char digest hint travels in a separate `prior_digest_short`
  field. Renderer surfaces the hint in the warning.
- **`POST /api/updates/bulk` accepted non-list `ids`** — `"ids": "abc"`
  silently iterated characters. Now strict `isinstance(list)` +
  500-item cap before any DB writes.
- **`dismiss_stale_interview_questions` set `answered_at`** for
  auto-dismissed rows — conflated the field's semantics. Now stays
  NULL. Negative `older_than_days` is a no-op (was silently
  dismissing future-dated rows).
- **`image_pins` glob shadowed exact-match keys** when the user's
  YAML ordering put the glob first. Now exact-match keys win
  unconditionally; globs are tier 2.
- **`image_pins` operator path was overly conservative on parse
  failure** — silently blocked legitimate updates. Now returns
  `allowed=True` with an explanatory reason when `packaging` can't
  parse a version, so the user sees the skip rather than a phantom
  veto.
- **`engine` mutated the plugin's `Update.context` dict in place**
  when injecting `pin_violation`. Now replaces with a new dict so
  plugin-cached contexts stay clean.
- **`notes_git.auto_commit` crashed on non-`Name <email>` author
  strings** (the default form works, but callers passing `"javi"`
  hit IndexError on `split('<')[1]`). Now accepts free-form names
  with a safe default email.
- **`sidecars` regex rejected the `container:/name` form** docker
  sometimes stores. Allowed-char set now includes `/`.
- **`watched_enrich` mypy errors** — typo `base64.binascii.Error` →
  `binascii.Error`; `_fetch_json` return-type union not narrowed
  before `_decode_readme_body`.

### Internal

- 1075 → 1118 tests (+43), ruff clean, 0 mypy errors against 122 files.
- 1 new config field (`sources.docker.detect_sidecars`), 1 new config
  block (`AutoApplyConfig`), 1 new DB table (`heartbeats`), 1 new
  Database mixin (`HeartbeatsMixin`).
- All 4 critical findings from the review are fixed with regression
  tests in `tests/test_polish_v047.py`.

## v0.4.6 — 2026-06-03

Four leverage items. Each one extends an existing pipeline rather than
introducing a new surface — the project is now wide enough that
"connect the existing dots" pays better than "add another tool".

### Added

- **Release-cadence stagnation detector** (`release_cadence.py`,
  `sources.docker.release_cadence`). Computes the median
  days-between-releases over the last ~30 releases per repo and flags
  when the current gap exceeds the median by ≥2× (medium) / ≥4×
  (high). Surfaces as `Update.context.release_cadence` and a new
  prompt rule that escalates severity + injects "no release in <N>d
  (median cadence: <M>d)" into the summary. Distinct from
  `repo_health` — that one cares about commits; this one cares about
  releases (the relevant signal for the average user).
- **Notes git auto-commit** (`notes_git.py`). When `notes_dir` is a
  git working tree, every curator write auto-stages + commits the
  changed file with a stable `curator: <kind>=<name> [update=<id>]`
  message. `git log notes/mealie.md` becomes a per-service history;
  weekly diffs are a single `git log --since=7d` away. Best-effort:
  no git binary / no .git dir / nothing to commit are all silent
  no-ops. Wired into both the full-rewrite curator (`curator/core.py`)
  and the incremental hook (`curator/incremental.py`).
- **MCP additional tools** — `audit`, `rollback_recipe`, `history_csv`,
  `health_check_results`. The MCP server now mirrors most of the CLI
  for agent use; the only commands still CLI-only are `curate` (it's
  side-effectful and slow) and `csi` (long-running LLM call).
- **Bulk action API** — `POST /api/updates/bulk` with
  `{"ids": [...], "status": "applied"|"dismissed"|"failed"}`. Applies
  the per-id treatment (including the post-update health-check queue
  for applied/docker rows) and returns `{applied, not_found,
  not_found_count}`. Pre-flight gate intentionally NOT applied — bulk
  action implies the user already reviewed the list.

### Internal

- 1045 → 1075 tests (+30), ruff clean, 0 mypy errors against 118 files.
- 1 new config field (`sources.docker.release_cadence`), 1 new prompt
  rule (release_cadence severity escalation), 4 new MCP tools.
- 1 new module (`notes_git.py`), 1 new pipeline (`release_cadence.py`).

## v0.4.5 — 2026-06-03

Four UX / operations features. All opt-in via their own config blocks
or CLI flags.

### Added

- **Notification quiet hours** — every push output
  (Telegram/Discord/Ntfy/Gotify) gains `quiet_hours: "23:00-07:00"`,
  `quiet_hours_timezone: "Europe/Madrid"`, and
  `quiet_hours_bypass_severity: "critical"`. During the window, push
  notifications queue into `pending_dispatches` (the same table the
  parity gate uses) and replay on the next ungated scan. Critical
  messages escape by default; flip the bypass field to `""` to
  enforce the window unconditionally. Window crosses midnight when
  start > end. Implemented as a mixin on each output config so the
  schema-driven settings renderer picks it up without per-output
  template work.
- **CSV history export** — `homelabsage history --csv -o file.csv` (or
  `-o -` for stdout). Dumps every `updates` row with severity, status,
  breaking_changes (joined with ` | ` for single-line cells), and the
  recommended_action. Lets the user pivot the audit log in
  Numbers/Excel/Sheets without reaching for `sqlite3`.
- **Image-pin enforcement** — `image_pins: {pins: {subject: spec}}`.
  When a detected update's `new_version` crosses the pin, the analyzer
  sees `Update.context.pin_violation` BEFORE running the prompt; a new
  prompt rule overrides the verdict to `hold` and frames the
  recommendation around the user-asserted constraint. Specs accept
  exact-match (`8.5.2`), glob (`8.*`), and operator (`<=2026.5`,
  `<2026.5`, `>=2026.5`, `==2026.5`) forms; the subject key itself can
  be a glob so `homeassistant/*: <=2026.5` works.
- **Stale interview cleanup** — `homelabsage interview cleanup
  --days N` dismisses PENDING interview questions older than N days.
  Cron-friendly + `--dry-run` mode that prints what would change
  without touching the table. Useful when the curator emitted a
  question the user never intends to answer (one-off containers,
  throwaway experiments).

### Internal

- 1004 → 1045 tests (+41), ruff clean, 0 mypy errors against 116 files.
- 1 new config block (`image_pins`), 3 new fields on each of the 4
  push outputs (`quiet_hours`, `quiet_hours_timezone`,
  `quiet_hours_bypass_severity`).
- 1 new prompt rule (`pin_violation`).
- 2 new CLI commands (`history`, `interview cleanup`).
- 1 new DB method (`dismiss_stale_interview_questions`).

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
