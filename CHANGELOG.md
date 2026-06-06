# Changelog

All notable changes ship here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely. Dates are UTC.

## v0.11.5 — 2026-06-06

Refactor pass #6: **`audit.build_report` cascade → registry**. Audit
finding #4 closed. The 17-block `findings.extend(_X_findings(...))`
cascade is gone; 8 inline `from .X import Y` imports hoisted to
module top. Adding a new per-update detector is now a one-line edit
to `_PER_UPDATE_DETECTORS`.

### Refactored

- `_PER_UPDATE_DETECTORS: list[Callable[[AnalyzedUpdate], …]]` —
  registry of 11 unconditional detectors. Iterated once per
  non-applied/dismissed update.
- `_collect_per_update_findings(cfg, item)` — runs every registered
  detector + tag-lag (config-gated, so it stays outside the
  unconditional registry).
- `_collect_state_findings(cfg, db, backup_report)` — runs the
  non-per-update probes (pending dispatches, parity, backup health,
  health checks, log anomalies, compose lint, disk pressure, compose
  override, env perms, recurring failures).
- `_state_parity(cfg)` + `_state_backup_health(cfg, backup_report)`
  extracted to named helpers (the only state probes with non-trivial
  setup).
- `_apply_audit_mutes(db, findings)` — extracted from `build_report`.
- 8 inline imports hoisted: `audit_alert`, `audit_history` (×2),
  `backup_health`, `compose_lint`, `compose_override`,
  `disk_pressure`, `env_perms`, `tag_lag`. None were anti-circular —
  verified by import graph.
- `build_report` now reads as a linear script: per-update loop →
  state findings → mutes → sort + counts → AuditReport.

### Internal

- 1725 → 1725 tests (no behavioural change). Ruff clean, 0 mypy
  errors against 183 source files.
- Six refactor passes since v0.10.7 close 9 of the 10 code-quality
  findings from the 6-jun audit. Only #9 remains (tests organised
  by release file instead of by feature) — explicitly deferred as
  a separate sprint per the audit memo; doesn't block v1.0.

## v0.11.4 — 2026-06-06

Refactor pass #5: **`engine.run_once` split**. Audit finding #3
closed. The 230-LOC per-scan god method that did gate-checking,
plugin scanning, per-update analysis, dispatch, batching, and
heartbeating in one inline blob is now a slim ~50-LOC orchestrator
that calls six named sub-stages. Behaviour preserved bit-for-bit.

### Refactored

- `engine.run_once()` reads as a linear script: scan-window gate →
  parity probe → per-plugin loop → per-update (analyze → dispatch)
  → batch finalise → heartbeat.
- `_maybe_skip_scan() -> dict | None` — combined scan-window +
  LLM-health early-return.
- `_check_parity_gate() -> bool` — RAID mdstat probe with logging,
  returns whether push-to-output is gated.
- `_batch_threshold() -> Severity | None` — resolves the batching
  severity floor from config.
- `_analyze_single(update, stats)` — image pin + dedup + LLM call +
  explainer + usage record + persist + auto-apply + curator hook.
- `_dispatch_single(analyzed, push_gated, batched, batch_threshold)`
  — per-output snooze/parity/quiet/batching gates.
- `_finalise_batch(batched, batch_threshold, push_gated)` —
  end-of-scan low-severity rollup flush.

### Internal

- 1725 → 1725 tests (no behavioural change). Ruff clean, 0 mypy
  errors against 183 source files.
- Five refactor passes since v0.10.7 close 8 of the 10 code-quality
  findings from the 6-jun audit. Remaining: #4 (audit.build_report
  cascade → registry), #9 (tests by feature instead of by release).

## v0.11.3 — 2026-06-06

Refactor pass #4: **`routes_updates.py` split by surface**. The
790-LOC module with 25 routes is now three single-purpose files —
HTML rendering, JSON API, and external webhook receivers. No
behavioural change; every URL still resolves to the same handler.

### Refactored

- `routes_updates.py` (790 → 417 LOC): HTML routes only — index,
  search page, snooze/star/note HTMX endpoints, status apply +
  preflight gate, CSV download, `/run`.
- `routes_updates_api.py` (NEW, 206 LOC): all 11 `/api/updates/*`
  JSON endpoints (`list`, `note` read+write, `star`, `snooze`,
  `recurring-failures`, `starred`, `search`, `bulk`,
  `snoozed` GET+DELETE).
- `routes_inbox.py` (NEW, 207 LOC): the two webhook receivers
  (`/api/inbox/{source}`, `/api/webhook/github-release`). These
  are HMAC-gated rather than Basic-Auth-gated and don't conceptually
  belong with the dashboard at all.
- `web/__init__.py` registers the three modules in order. URLs
  unchanged.

### Internal

- 1725 → 1725 tests (no net change; same handlers in the same
  URL shapes). Ruff clean, 0 mypy errors against 183 source files.
- Three refactor passes since v0.10.7 (Output ABC + mcp split +
  hasattr strip + routes split) close 7 of the 10 code-quality
  findings from the 6-jun audit. Remaining: #3 (engine.run_once
  230-LOC god function), #4 (audit.build_report cascade →
  registry), #9 (tests by feature instead of by release).

## v0.11.2 — 2026-06-06

Refactor pass #3: **dead-code cleanup**. All 41 `hasattr(db, X)`
defensive guards stripped — every method probed lives on a mixin
that `Database` always inherits. No behavioural change.

### Refactored

- 17 sites in `mcp_tools.py` (`_tool_*` impls) lose their
  `if not hasattr(db, …): raise/return` blocks. The mixin
  composition in `db/__init__.py:48` guarantees the methods exist.
- 12 sites in `web/routes_updates.py` lose `hasattr(db, …) else`
  ternaries — the view-context block goes from 12 fragmented lines
  to a tight comprehension.
- 5 sites in `web/routes_audit.py`, `web/routes_stack_health.py`,
  `web/routes_metrics.py`, `cli/status.py` lose their fallbacks.
- 4 sites in `audit.py` lose the noop branches around
  `list_recent_health_checks`, `list_recent_log_anomalies`,
  `list_recurring_failures`, `active_audit_mute_keys`.
- The legacy "Database stub" `toggle_starred` fallback in
  `routes_updates.toggle_star_html` deleted: there is no stub DB,
  the read-then-write fallback was dead.

### Why this is safe

- `Database(UpdatesMixin, InterviewMixin, WatchedMixin, PendingMixin,
  ExplainersMixin, UsageMixin, HealthCheckMixin, LogAnomalyMixin,
  HeartbeatsMixin, AuditMutesMixin)` (db/__init__.py:48) is the
  single concrete instance in the codebase.
- mypy now catches typos in DB method names that were previously
  silently swallowed by `hasattr(db, "typoed_name")` → False branch.

### Internal

- 1725 → 1725 tests (no net change; behaviour preserved).
- Ruff clean, 0 mypy errors against 181 source files.
- The three refactor passes (v0.11.0 ABC + v0.11.1 mcp split +
  v0.11.2 hasattr strip) together close 6 of the 10 code-quality
  findings from the 6-jun audit: #1 (output dup), #2 (mcp god
  module), #6 (hasattr guards), plus the related sub-findings #8
  (batch dup), #10 (settings_test mirror), and most of #7 (inline
  imports). Findings #3-#5 + #9 (engine god-fn, audit cascade,
  routes_updates split, test reorg) remain for future passes.

## v0.11.1 — 2026-06-06

Refactor pass #2: **`mcp.py` split**. The 1831-line god module is
now 153 LOC of envelope + dispatch + route registration, with the
50 `_tool_*` impls and the 660-line `TOOLS` JSON-Schema descriptor
moved to a sibling `mcp_tools.py`. No behavioural change.

### Refactored

- `mcp.py` (1831 → 153 LOC): JSON-RPC error helpers, `dispatch()`,
  `register_mcp_routes()`. Imports only `TOOLS` from the new sibling.
- `mcp_tools.py` (NEW, 1715 LOC): all 50 `_tool_*` impls, the
  `TOOLS` registry, and the impl-only helpers
  (`_summarise_update`, `_full_update`, `_run_coro`). Tools cluster
  by section comments; the JSON-Schema descriptor still lives in
  one big literal for now (a future refactor can co-locate each
  descriptor with its impl via a decorator).

### Test changes

- `test_v062.test_mcp_run_coro_helper_handles_nested_loop` imports
  `_run_coro` from `mcp_tools` (was `mcp`).
- Everything else untouched — `dispatch`, `TOOLS`, and
  `register_mcp_routes` are still importable from `mcp` at the
  original paths.

### Internal

- 1725 → 1725 tests (no net change, one import path updated).
  Ruff clean, 0 mypy errors against 181 source files.
- `mcp.py` is now navigable end-to-end on one screen; `mcp_tools.py`
  is mechanical line-by-line (each tool is independent).

## v0.11.0 — 2026-06-06

Refactor pass #1 from the post-v1.0-punch code-quality audit:
**Output ABC consolidation**. 11 push outputs lose ~80 LOC of
boilerplate by hoisting the shared severity gate + httpx wrapper +
sanitised failure log to the base class. No behavioural change.

### Refactored

- `outputs/__init__.Output` gains three opt-in helpers:
  - `_severity_passes(item)` — returns True iff
    `item.analysis.severity >= self._min`. Replaces the 3-line check
    that was duplicated in every subclass.
  - `_log_push_failure(item, exc)` — emits the
    `"<id> push failed for X: <type> (status N)"` log line via the
    centralised `safe_error` redactor. Single source of truth for
    failure logging.
  - `_push_json(url, *, item, json, params=, headers=, timeout=)` —
    fire-and-forget JSON POST that wraps the httpx try/except. 6 of
    the 11 outputs (Discord, Telegram, Gotify, Slack, MSTeams,
    Webhook) drop ~6 LOC each by using it.
- Outputs with non-JSON shapes (ntfy text/plain, pushover form,
  apprise library call, smtp via smtplib) keep custom sends but
  share `_log_push_failure` for consistent redaction.
- Notion's PATCH+POST fallback path stays bespoke (the v0.10.3
  404-self-heal logic isn't a fit for a generic helper).

### Test changes

- `test_every_output_imports_safe_error` (defensive marker test from
  v0.10.0) replaced by `test_every_output_inherits_sanitized_base`
  — same intent, asserts via `issubclass(cls, Output)` + inspect
  on the centralised `_log_push_failure` source.
- `test_telegram_log_does_not_leak_token` rewired to drive
  `_log_push_failure` directly through a real `TelegramOutput`.
- Two webhook tests updated to monkeypatch
  `homelabsage.outputs.httpx.AsyncClient` instead of the per-module
  alias that no longer exists.

### Internal

- 1725 → 1725 tests (no net add; one swap to the new defensive
  guard). Ruff clean, 0 mypy errors against 180 source files.
- 9 of 11 output modules drop their `httpx` import. `safe_error` is
  imported in exactly one place (`outputs/__init__.py`) instead of
  11.

## v0.10.7 — 2026-06-06

Polish pass for the 5 Nits from the v1.0 punch list. N1 + N2 are
documented (no functional change needed); N3, N4, N5 ship as small,
testable improvements.

### Fixed (Nit)

- **N3 — `db.purge_pending_dispatches_older_than(days=N)`** added so
  a misconfigured push output during a long parity window can be
  evicted via a one-liner instead of `clear_pending_dispatches` (the
  panic button). Returns the count purged. `days <= 0` is a no-op.
- **N4 — Rule-7 fallback regex anchored.** `is_purpose_fallback`
  now uses `fullmatch` so a coherent note that just *mentions* the
  fallback template (e.g. "fixed the 'no purpose stated yet — fill
  in' confusion") is no longer misclassified as a bailout.
- **N5 — `list_pending_dispatches(limit=N)`** gains a default cap
  (1000) so an unbounded scan from MCP doesn't pull megabytes of
  queue at once.

### Documented (Nit)

- **N1 — Curator docker client lifecycle.** The engine doesn't hold
  a curator instance today, so the existing `Curator.close()` is
  the only required cleanup. Flag stays in the punch list as a
  hook for a future `/curate` web endpoint.
- **N2 — `_run_coro` thread-spawn fallback** is now correctly
  understood. Since v0.10.1's `asyncio.to_thread(dispatch, …)` wrap,
  HTTP callers always take the `asyncio.run` branch — the thread
  spawn is defensive for direct importers that call dispatch
  from inside their own async code. Docstring updated.

### Internal

- 1714 → 1725 tests (+11). Ruff clean, 0 mypy errors against 180
  source files.
- v1.0 punch list status: **all 5C + all 12I + 3/5N closed**. N1 and
  N2 are no-op-today documentation entries. Branch tagged-ready for
  v1.0.0 milestone.

## v0.10.6 — 2026-06-05

Webhook output secret-scrub + KV-redact tightening. Closes I8 + I11
from the v1.0 punch list — every Important finding now closed.

### Fixed (Important)

- **I8 — Webhook output scrubs `Update.context` and analyzer text.**
  Before: anyone with the webhook URL received the full inventory
  of homelab secrets (`env` dict from docker, `puid_pgid`, raw
  `release_notes`) plus any tokens the LLM hallucinated into
  `summary`/`breaking_changes`/`recommended_action`. Now: the
  envelope runs through `redact_context` (walks the tree, masks
  secret-key values, scrubs every string) and `redact_text` is
  applied to all three analyzer fields. Safe config flags survive
  (`LOG_LEVEL=info`, `puid_pgid=1000:1000`).
- **I11 — KV-redact regex skips obvious config flags.** Before:
  `OBSERVABILITY_PRIVATE=true`, `AUTH=public`, `AUTH_PORT=8080`,
  `SESSION=on` all got redacted, degrading LLM analysis with
  `<redacted-by-homelabsage>` placeholders where the real value
  was harmless. Now: values matching a small allowlist
  (`true|false|yes|no|on|off|none|null|nil|auto|default|enabled|
  disabled|public|private|0|1`) OR a short pure-numeric token
  (1-5 digits) pass through untouched. Real secrets stay redacted.

### Internal

- 1702 → 1714 tests (+12). Ruff clean, 0 mypy errors against 180
  source files.
- v1.0 punch list status: **all 5 Critical + all 12 Important
  findings closed**. Only the 5 Nits remain (all opt-in, marked
  for future v1.x consideration). The branch is now eligible for
  v1.0 milestone when the user is back from vacation.

## v0.10.5 — 2026-06-05

Last critical from the v1.0 punch list (C4) plus log allocation bound
(I12). After this release every Critical from the audit is closed.

### Fixed (Critical)

- **C4 — `restart_freq` no longer flags stable containers as critical
  after a manual restart.** Before: a container with 47 lifetime
  crashes spread across 90 days, restarted 5 min ago, computed
  `rate = 47 / 0.083 = 564/h` → critical. The detector now requires
  **at least 1 hour of uptime** before emitting any rate-based
  finding (`min_uptime_hours=1.0`, operator-tunable). Short-window
  flapping that's real will resurface the next scan once uptime
  passes the floor. A precise fix using the container event log is
  deferred to v1.x.

### Fixed (Important)

- **I12 — `fetch_docker_logs` now caps both line count and per-line
  bytes.** Added `max_lines=4000` (existing CSI cap, parameterised)
  and `max_line_bytes=4096` (new). `log_anomaly.scan_container` now
  passes `max_lines = max(500, lookback_minutes * 200)` instead of
  inheriting the 4000 default — it only counts regex hits and
  doesn't need the LLM-sized tail. A 30 KB JSON log line per entry
  no longer materialises into a 30 KB string in memory.

### Internal

- 1692 → 1702 tests (+10). Ruff clean, 0 mypy errors against 180
  source files.
- All 5 Critical findings from the v1.0 punch list are now closed
  (C1, C2, C3, C4, C5). Remaining for v1.0 promotion: I8, I11 (plus
  the 5 Nits, opt-in).

## v0.10.4 — 2026-06-05

Detector false-positive cleanup — closes I6 + I7 from the v1.0
punch list.

### Fixed (Important)

- **I6 — `log_anomaly` regex no longer counts bare `err`/`crit`
  words.** The old pattern included the 3-char `err` and 4-char
  `crit` alternatives. In real logs these match noisy non-errors:
  Go's `err = nil`, `if err != nil`, syslog severity tokens like
  `priority=crit`. Real errors always emit the full English form
  somewhere; the tighten drops noise without losing signal. Verified
  by 4 new tests (bare-err lines now count 0, real ERROR/FATAL/
  PANIC/CRITICAL still count 6/6).
- **I7 — `compose_lint bind_no_user` rule pinned to actual LSIO
  images.** The old heuristic fired on *any* compose service that
  bound `/data` or `/config` and had no `user:` — that's >90% of
  homelab containers, including `redis:7-alpine`, `postgres:16`,
  and every random tag — overwhelming noise. Now scoped to
  `lscr.io/linuxserver/...` and legacy `linuxserver/...` images
  only, where PUID/PGID actually applies. Detail message updated to
  cite the matched image. Existing `bind_no_user` test moved to use
  an LSIO image.

### Internal

- 1683 → 1692 tests (+9). Ruff clean, 0 mypy errors against 180
  source files.

## v0.10.3 — 2026-06-05

Notion output hardening — closes I4 + I5 from the v1.0 punch list.

### Fixed (Important)

- **I4 — `Analysis.summary` is now `redact_text`'d before reaching
  Notion.** `secret_guard.redact_text` already ran pre-LLM; the gap
  was that a Qwen-Abl run could *hallucinate* `GITHUB_TOKEN=ghp_…`
  out of a release-note snippet and the value would land in Notion
  in cleartext. The post-LLM scrub closes that loop. Clean
  summaries (no marker, no value pattern) pass through untouched.
- **I5 — Stale `notion_page_id` self-heals on 404.** When the
  cached page_id 404s on PATCH (page was manually deleted in
  Notion), we now: clear the id in memory + DB, fall through to a
  fresh POST in the same call, persist the new id. Before, the
  same 404 fired every scan forever. Non-404 PATCH failures still
  log and leave the id intact for retry — we don't risk creating
  duplicates on transient Notion outages.

### Internal

- 1678 → 1683 tests (+5). Ruff clean, 0 mypy errors against 180
  source files.
- `db.set_notion_page_id(id, None)` is now explicitly typed as
  `str | None` — was `str` only, the new clear-on-404 path relies
  on it.

## v0.10.2 — 2026-06-05

DB concurrency primitive + apprise unblocked. Closes I1 + I2 from
the v1.0 punch list.

### Fixed (Important)

- **I1 — `db.transaction()` context manager** added for atomic
  multi-statement writes. Holds a reentrant `threading.RLock` for
  the duration of the block AND wraps the body in `BEGIN
  IMMEDIATE`/`COMMIT` (autocommit is off inside the block).
  Exceptions roll back; reentry on the same thread doesn't
  deadlock. The existing per-statement writes still rely on WAL +
  SQLite's per-statement locking — this primitive is for future
  callers that need read-modify-write atomicity (and the
  serialization test in `test_v0102.py` proves concurrent
  transactions never observe half-applied state).
- **I2 — `Apprise.notify` no longer blocks the engine loop.** The
  library's public `notify()` is documented sync and waits for
  every fan-out; 5 URLs × 5 s timeout = 25 s freeze of the engine
  loop. Now wrapped in `asyncio.to_thread`. Test sets up a fake
  apprise that sleeps 150 ms in `notify`, runs the output via
  `asyncio.run`, asserts the call landed on a *different* thread
  than the event loop.

### Internal

- 1671 → 1678 tests (+7). Ruff clean, 0 mypy errors against 180
  source files.
- `Database.__init__` gains a `_lock: threading.RLock` field and
  `Database.transaction()` returns a `sqlite3.Connection` for the
  duration of the block. No call site retrofitted in this release —
  the existing single-statement writes were already safe.

## v0.10.1 — 2026-06-05

MCP layer hardening — closes C2, C5 and I10 from the v1.0 punch list.

### Fixed (Critical)

- **C2 — MCP dispatcher no longer blocks the event loop.** Many
  tools do blocking I/O (docker SDK `images.list`, TLS probe loops,
  compose-graph walks, sync SQLite reads). The sync `dispatch()`
  was called directly from the async `mcp_post` route, freezing
  the entire web UI + scheduler for the duration of a slow tool.
  `mcp_post` now awaits `asyncio.to_thread(dispatch, …)` — a hung
  tool ties up one worker thread instead of the loop. New regression
  test fires 3 parallel 0.3 s tool calls and asserts the whole
  batch finishes in < 0.75 s (vs 0.9 s if serial).
- **C5 — Catch-all MCP error no longer echoes `str(e)`.** Exception
  messages routinely carry secrets (SQLite paths, HTTP URLs with
  tokens, SSH host strings, container env vars). The default
  catch-all returned
  `f"tool {name!r} failed: {e}"` to the client. Now it returns
  `tool {name!r} failed ({type}) — see server logs`, reuses
  `safe_error()` from v0.10.0, and writes the full trace to
  server logs only.

### Fixed (Important)

- **I10 — `tls_check_run` MCP tool caps caller-supplied URL list at
  50.** Each probe is ~10 s serial; a bored agent passing 1000
  URLs would block the dispatcher for ~3 h. Hard cap, no error —
  drop the excess silently with the same shape the caller expected.

### Internal

- 1666 → 1671 tests (+5). Ruff clean, 0 mypy errors against 180
  source files.
- `dispatch()` stays sync + pure — the threading shim lives only in
  the HTTP route so the function remains trivially unit-testable.

## v0.10.0 — 2026-06-05

Start of the v1.0 punch-list sprint — closing the two highest-severity
findings from the pre-v1.0 audit: a process-wide socket-timeout
race and bot-token leakage into output failure logs.

### Fixed (Critical)

- **C1 — DNS probe no longer poisons process-wide socket timeout.**
  `dns_check._temp_timeout` used `socket.setdefaulttimeout()`, which
  is global; a concurrent httpx / docker SDK / ssl / smtplib call
  in another thread silently inherited the 3 s timeout while a
  probe was in flight, and two interleaved probes could restore
  the wrong baseline. Replaced with a module-level
  `concurrent.futures.ThreadPoolExecutor` (4 workers, named
  `hls-dns`) — `future.result(timeout=N)` bounds the caller's wait
  without touching any global state. Hung lookups stay running in
  the worker thread until the system resolver gives up; we just
  detach our caller. New test
  `test_dns_probe_concurrent_safe` runs two threads probing in
  parallel and asserts the global timeout stays exactly at the
  pre-call value.
- **C3 — Output exception logs no longer leak bot tokens.**
  `httpx.HTTPStatusError.__str__()` includes the full request URL.
  Telegram puts the bot token in the URL path (`/bot{token}/…`),
  Gotify in the query string (`?token=…`); Discord, Slack, MSTeams,
  Pushover, Apprise webhooks have the secret IN the URL. A single
  4xx with default logging routed → token in the log file, in
  syslog-mcp, logspout, restic backups to Infomaniak. New shared
  helper `outputs._errlog.safe_error(e)` returns
  `ExceptionType (status N)` and never includes the URL / body.
  Wired into all 11 outputs (`telegram, gotify, discord, ntfy,
  slack, msteams, pushover, webhook, apprise, smtp, notion`) plus
  the 4 batch dispatchers. Parametrized test asserts every output
  module imports the helper so a future refactor can't regress
  one channel silently.

### Internal

- 1646 → 1666 tests (+20), ruff clean, 0 mypy errors against 180
  source files (new `outputs/_errlog.py`).
- No behavioural changes outside the failure path — happy-path
  push semantics are byte-identical.

## v0.9.10 — 2026-06-05

Three pre-existing CLI/MCP surfaces now reachable from the GUI: full
substring search, CSV download of the entire updates table, and
in-line free-text user notes per row.

### Added

- **HTML search**: new `GET /search?q=…` route renders the same
  layout as `/` filtered to substring matches across subject,
  summary, breaking changes and user notes (driven by
  `db.search`, capped at 200). The header on every page now
  carries a search box that points there.
- **CSV download**: `GET /api/updates/history.csv` streams the
  same payload as `homelabsage history -o file.csv`. One-click
  download from the header (`⬇ CSV`).
- **Inline user notes**: new `Note` column per row. Empty cells
  show `add note`; filled cells show the text + `edit`. Clicking
  swaps to a textarea (HTMX `GET /updates/{id}/note/edit`);
  `save` posts to `/updates/{id}/note` and swaps back; `cancel`
  drops the form. Notes are HTML-escaped (`<script>` → `&lt;script&gt;`).

### Internal

- 1631 → 1646 tests (+15), ruff clean, 0 mypy errors against 179 files.
- New `db.list_user_notes() -> dict[str, str]` mixin returns only
  rows with non-empty notes — one query for the index instead of
  one per row.
- The `_note_cell_html` / `_note_edit_form_html` helpers are
  module-level so the swap fragment matches the template's
  initial render byte-for-byte.

## v0.9.9 — 2026-06-05

UX + a11y polish on the updates table — last lap before the v1.0
code-quality audit. No behavioural changes to the engine or
detectors; all surface improvements on `/`.

### Added

- **Custom snooze datepicker.** Beside the 7/14/30/90 dropdown,
  each unsnoozed row now has a `<input type="date">` for
  "snooze until X". HTMX POSTs to `/updates/{id}/snooze/until`,
  YYYY-MM-DD only, past/today silently clears (defensive). Invalid
  date → 400, unknown id → 404.
- **Inline `explain` link** per row when an analysis explainer
  exists. Sends the user to `/updates/{id}/explain` to read the
  exact LLM prompt + raw response that produced the verdict —
  surfaces the audit trail without making the user remember the
  URL. Cheap bulk query `db.list_explained_ids()` keeps the page
  render to one extra SELECT instead of one per row.

### Accessibility

- **Star button now ships `aria-label` + `aria-pressed`** in both
  the initial template render and the HTMX toggle response.
  Screen-reader users hear `Star <subject>` / `Unstar <subject>`
  instead of just "button".
- **Snooze controls carry `aria-label`** identifying which row +
  what dimension they act on (`Snooze duration for <subject>`,
  `Snooze <subject> until`).

### Internal

- 1620 → 1631 tests (+11), ruff clean, 0 mypy errors against 179 files.
- New `db.list_explained_ids() -> set[str]` mixin helper. New
  `POST /updates/{id}/snooze/until` endpoint reuses the same
  `_snooze_cell_html` swap target as `/snooze/quick`.

## v0.9.8 — 2026-06-05

Last GUI surface gap closed before the v1.0 audit sprint: muting an
audit finding no longer requires the CLI or MCP — it's a one-click
inline button on `/audit`.

### Added

- **Inline mute form per audit finding.** Each row in `/audit` now
  carries a tiny `mute` button with an optional reason field. HTMX
  POSTs to `/audit/mute` with the finding's fingerprint
  (`category` + `source_kind` + `source_ref`), the form swaps in
  place to a `muted ✓` badge, and the finding is gone from the next
  report build.
- **Active-mutes panel** at the top of `/audit` (collapsible
  `<details>`). Lists every active mute with category pill, source
  fingerprint, reason and expiry. Each row has an HTMX `unmute`
  button that POSTs to `/audit/mute/remove` and vanishes the row in
  place.
- **`/usage` link in the global nav.** The LLM token-usage page
  shipped in v0.4.0 was reachable only by URL — now it's one click
  away from any page.

### Internal

- 1613 → 1620 tests (+7), ruff clean, 0 mypy errors against 179 files.
- Both new endpoints reuse the existing `db.add_audit_mute` /
  `db.remove_audit_mute` helpers; no new DB shape.

## v0.9.7 — 2026-06-05

Security + correctness fixes from the v0.9.5 / v0.9.6 bug-hunt.

A code-review pass against the two preceding GUI releases caught
**two credential-leakage paths** introduced when the v0.9.5
release exposed 22 new settings blocks: list and dict fields
holding secrets (`outputs/apprise.urls`, `web/auth.api_keys`,
`outputs/webhook.headers`, plus several `webhook_url` strings
whose name didn't match the secret-marker heuristic) round-tripped
in cleartext through both the JSON API and the HTML form.

### Security

- **Secrets in list/dict shapes are now masked.** `_mask_secrets`
  preserves outer shape: non-empty string → `"***"`, populated
  list → `["***", …]`, populated dict → `{k: "***", …}`. The HTML
  form renderer also runs through the same gate so the
  `<textarea>` for `urls` / `headers` no longer carries
  cleartext.
- **Explicit `ui_secret` schema-extra annotation** on
  `outputs/{apprise,discord,msteams,slack,webhook}` webhook URLs
  + `apprise.urls` + `webhook.headers` + `pushover.user_key`.
  These didn't match the substring marker heuristic
  (`webhook_url` has no marker; `urls` is plural). New
  `_field_is_secret(name, prop_schema)` helper consults both
  signals.
- **Blank submit preserves a configured list/dict secret.** The
  existing string-secret protection now applies uniformly — an
  empty `urls` textarea on save no longer wipes existing entries.

### Fixed

- **Atomic star toggle.** `db.toggle_starred()` does a single
  `UPDATE … SET starred = 1 - COALESCE(starred, 0) … RETURNING
  starred`, eliminating the read-then-write window where two
  simultaneous clicks could both observe False and both write
  True (one click lost).
- **404 on unknown `update_id`.** `POST /updates/{id}/star/toggle`
  and `POST /updates/{id}/snooze/quick` now return 404 instead of
  silently rendering a happy button for a row that doesn't exist.
- **Pill counts use `COUNT(*)`.** `db.count_starred()` and
  `db.count_snoozed()` replace `len(list_starred(limit=500))`, so
  the Starred/Snoozed pill labels stay accurate beyond 500 rows.

### Added

- New `test_v097.py` (14 tests): masking on `apprise.urls`,
  `web/auth.api_keys`, `webhook.headers`, `msteams.webhook_url`
  in both JSON and HTML form; blank-submit round-trip preserving
  list AND dict secrets; 404 on bogus id for star + snooze; the
  atomic-toggle invariant; `days=-5` clears defensively; pill
  counts via COUNT(*).

### Internal

- 1599 → 1613 tests (+14), ruff clean, 0 mypy errors against 179 files.
- No new dependencies. `redact._field_is_secret` is the single
  source of truth shared by the JSON API, the HTML form renderer
  and the form save path.

## v0.9.6 — 2026-06-05

In-line star + snooze from the updates table (no page reload).

The updates index now renders a row of GUI controls per finding so
the user no longer has to drop to CLI or the MCP to bookmark, mute
or filter. Built on HTMX so each click swaps just the affected
cell — no full-page rerender.

### Added

- **Star toggle button** per row (`★`/`☆`). HTMX `POST
  /updates/{id}/star/toggle` flips the `starred` state and returns
  a fresh self-targeting button so consecutive clicks chain
  correctly without re-fetching the page.
- **Snooze quick-select** cell per row. HTMX `POST
  /updates/{id}/snooze/quick` accepts a `days` form field (7/14/30/90
  preset dropdown) and writes a future ISO timestamp; `days=0`
  clears. The cell renders either a `💤 YYYY-MM-DD HH:MM + clear`
  pair when snoozed or a select dropdown when not — toggled in-place.
- **Starred / Snoozed pill filters** in the header row, alongside
  the existing severity counts. `/?filter=starred` and
  `/?filter=snoozed` narrow the table; the pill labels include
  live total counts pulled from the DB (not the filtered view).
- Unknown `filter=` values fall back to "all" (defensive).
- New integration test `test_v096.py` (11 tests) covering the
  toggle round-trip, the HTMX self-target contract, snooze
  set/clear/default, all three filter views, the pill-count
  invariant and per-row button rendering.

### Internal

- 1588 → 1599 tests (+11), ruff clean, 0 mypy errors against 179 files.
- `routes_updates.py` gains a module-level `_snooze_cell_html`
  helper shared by the HTMX endpoint and matching the template's
  inline form, so the swapped HTML stays identical to the initial
  render.

## v0.9.5 — 2026-06-04

GUI surface expansion — every config block reachable via web.

A background audit agent mapped the current GUI vs. the
`Config` dataclass and found **17 config blocks invisible to
the web UI** (everything added after v0.6.0). The schema-driven
form renderer already handles them; only the `SETTING_BLOCKS`
registry was missing the entries. This release adds them all.

### Added

- **22 new blocks in `SETTING_BLOCKS`** (now 38 total, was 16):
  - **outputs**: `apprise`, `smtp`, `slack`, `msteams`,
    `pushover`, `webhook`, `batching`
  - **runtime**: `scan_window`, `llm_health_gate`,
    `auto_apply`, `image_pins`, `compose_lint`, `tag_lag`,
    `tls_check`, `disk_pressure`, `audit_alerts`,
    `backup_health`, `health_check`, `log_anomaly`, `i18n`
  - **sources**: `fedora`, `scripts`
  - **auth**: `web/auth`
- New integration test `test_v095.py` (5 tests) asserting every
  registered block resolves via both `/api/settings/{block}`
  and `GET /settings/{block}` HTML, plus the index lists them
  all. Guards against regressions where someone adds a Pydantic
  block but forgets to register it in the UI.

### Internal

- 1583 → 1588 tests (+5), ruff clean, 0 mypy errors against 179 files.
- Zero new modules — pure registry wiring. The schema-driven
  HTML renderer auto-discovered every new field's type.

## v0.9.4 — 2026-06-04

Two new detectors + two MCP db-exposure tools (research backlog).

### Added

- **`oom_killed.py`** + **`sources.docker.detect_oom_killed`**
  (default on). Reads `State.OOMKilled`; surfaces as `high`
  severity audit finding (`oom_killed` category). Cite includes
  exit_code + finished_at. Catches the silent class where a
  container OOM'd last night and the user only finds out at the
  next OOM. Strips `0001-01-01` docker sentinel timestamps.
- **`network_mode_host.py`** + **`sources.docker.detect_network_mode_host`**
  (default on). Flags `HostConfig.NetworkMode == "host"` at `info`
  severity. Often intentional (Tailscale, Plex DLNA, mDNS) — the
  user mutes via `audit-mute add network_mode_host
  network_mode_host <subject>` when reviewed.
- **MCP `get_explainer(update_id)`**. Returns cached LLM
  prompt + raw response without re-running the LLM. Cheaper than
  the existing `explain` tool when the user asks "why?" twice.
- **MCP `list_heartbeats(limit)`**. Returns `{summary: {...},
  recent: [...]}` — 24h rolling success/fail + last N pings.
  Useful for diagnosing scan staleness.

### Internal

- 1567 → 1583 tests (+16), ruff clean, 0 mypy errors against 179 files.
- 2 new detector modules, 2 new MCP tools, 2 new docker config toggles,
  2 new audit categories (`oom_killed`, `network_mode_host`).

## v0.9.3 — 2026-06-04

Research-driven: wire dormant detectors into auditor + MCP chronicle.
A background research agent found the codebase had 14 detectors that
ran inside the docker plugin (attaching context for the LLM) but
never surfaced as first-class auditor findings. This release wires
the three highest-impact ones; more in v0.9.4+.

### Added

- **`restart_freq` → audit finding** (`restart_flapping` category).
  Surfaces containers crashing ≥0.25/h as a `medium`+ auditor row.
  Previously only the LLM analyzer saw the context.
- **`healthcheck_stale` → audit finding** (`healthcheck_stale`
  category). Containers RUNNING but with a red healthcheck for
  hours — restart-flapping detector misses these since the process
  is alive. Inherits the severity (≥72h critical, ≥24h high,
  ≥4h medium) from the detector.
- **`exposed_ports` → audit finding**. Collapses the per-port
  verdict list to one finding per container at the worst severity
  present, so a container with 12 exposed ports doesn't drown the
  audit. Top-5 ports cited.
- **MCP `get_chronicle(days)`**. Wraps `chronicle.build_chronicle`
  (CLI-only until now). Returns `{period_start, period_end,
  counts_by_kind, entries: [{when, subject, kind, headline,
  detail}]}`. Days clamped 1..365.

### Internal

- 1559 → 1567 tests (+8), ruff clean, 0 mypy errors against 177 files.

## v0.9.2 — 2026-06-04

Lookup helpers.

### Added

- **`homelabsage where-is <name>` CLI** + **`where_is.py`** module +
  **MCP `where_is` tool**. Finds where a service / container is
  declared in compose files. Returns file + line + project + a
  short YAML context excerpt. Match rules in priority: exact
  service_name, exact container_name, case-insensitive substring.
  Useful when a Dockge stack catalogue grows past memory.
- **MCP `update_diff(a, b)`**. Side-by-side compare of two
  analyzed updates. Returns `{ok, a, b, diff: {severity_changed,
  breaking_changes_added, breaking_changes_removed, summary_a,
  summary_b}}`. Use case: "did the rebase change the risk
  profile?" / post-mortem queries.

### Internal

- 1544 → 1559 tests (+15), ruff clean, 0 mypy errors against 177 files.

## v0.9.1 — 2026-06-04

- **`GET /api/audit/by-category?category=X&limit=N`**. Returns the
  full finding payload for a single category — drill-down
  companion to `/api/audit/categories`. Empty `category` returns
  empty; unknown category returns `count=0`. `limit` clamped to
  500.
- **MCP `system_info`**. One-call rollup: `version`,
  `updates_by_status`, `audit_total`, `audit_counts_by_severity`,
  `snoozed_total`, `audit_mutes_total`, `pending_dispatches`. The
  right shape for an agent's first probe.

### Internal

- 1538 → 1544 tests (+6), ruff clean, 0 mypy errors against 175 files.

## v0.9.0 — 2026-06-04 — milestone

Consolidates everything since v0.8.0. No new features — the v0.8.x
line shipped its own surface, this is the SemVer bump that marks
the audit-mute + housekeeping work as the new baseline.

### Summary of the v0.8.x line

| Version | Highlight                                                  |
| ------- | ---------------------------------------------------------- |
| v0.8.0  | `homelabsage features` CLI + v0.7 roll-up                  |
| v0.8.1  | Audit-finding mute list (db + filter + CLI + HTTP + MCP)   |
| v0.8.2  | mute `purge-expired` + `add-from-stdin` + MCP `audit_categories` |
| v0.8.3  | `/api/audit/categories` HTTP mirror + MCP `is_snoozed`     |
| v0.8.4  | Dangling-images detector + MCP                             |
| v0.8.5  | `homelabsage activity` CLI                                 |
| v0.9.0  | Milestone bump (no new features)                           |

### Internal

- 1538 tests carry forward, ruff clean, 0 mypy errors against 175
  source files.

## v0.8.5 — 2026-06-04

- **`homelabsage activity` CLI**. Tail recent updates (most-recent
  first) with status colour + severity. `--json` for one JSON
  object per line — pipes into `jq`. `--limit` clamped to 200.

### Internal

- 1534 → 1538 tests (+4), ruff clean, 0 mypy errors against 175 files.

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
