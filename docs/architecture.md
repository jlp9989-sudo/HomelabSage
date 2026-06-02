# Architecture

HomelabSage is a single Python process that runs four steps in a loop:

```
   ┌──────────────────────────────────────────────────────────────┐
   │  1. PLUGINS scan()                                           │
   │     docker / homeassistant / fedora / github_watched         │
   │     → list[Update]                                            │
   └──────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  2. CONTEXT enrichment                                       │
   │     NotesProvider (your markdown notes)                      │
   │     repo_health · alternatives · cve · cascade · puid_pgid · │
   │     image_size_growth · orphan_since_days                    │
   └──────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  3. LLM analyze() → Analysis                                 │
   │     severity · summary · breaking_changes · config_obsolete  │
   │     new_features_relevant · action_required · recommended    │
   └──────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  4. PERSIST + ROUTE                                          │
   │     SQLite (state.sqlite) → outputs (Notion + push channels) │
   └──────────────────────────────────────────────────────────────┘
```

The whole loop is in `src/homelabsage/engine.py::Engine.run_once`. Each
step is its own module. There's no shared mutable state between scans —
every scan re-instantiates plugins/outputs from the active `Config`.

## Module map

```
src/homelabsage/
├── cli/                CLI subcommands (one file per command)
│   ├── _common.py        shared Typer `app` + console + option singletons
│   ├── check.py          `homelabsage check`
│   ├── curate.py         `homelabsage curate` (+ `--system`)
│   ├── digest.py         `homelabsage digest`
│   ├── csi.py            `homelabsage csi <container>`
│   ├── analyse.py        `homelabsage analyse <github-url>`
│   ├── notify.py         `homelabsage notify-pending`
│   ├── watched.py        `homelabsage watched {add,list,toggle,remove}`
│   ├── interview.py      `homelabsage interview {list,answer,dismiss}`
│   └── …
│
├── config/             Pydantic-typed config tree
│   ├── llm.py            LLMConfig (+ multi-profile support at root)
│   ├── sources.py        Docker / HA / Fedora / scripts / github_watched
│   ├── outputs.py        Notion / Telegram / Discord / Ntfy / Gotify
│   ├── runtime.py        Scheduler / Digest / ParityGate
│   ├── storage.py        Storage / Notes / Curator
│   ├── web.py            WebConfig + Basic Auth
│   ├── _env.py           ${VAR} interpolation + minimal .env reader
│   └── __init__.py       Config root + load_config + get_active_llm_config
│
├── db/                 SQLite layer (mixin-composed Database class)
│   ├── schema.py         CREATE TABLE + forward migrations
│   ├── updates.py        upsert/get/list updates table
│   ├── interview.py      Rule 7 questions
│   ├── watched.py        watched_repos
│   ├── pending.py        pending_dispatches (parity-gate catch-up)
│   └── __init__.py       Database = mixin composition
│
├── plugins/            One file per source. scan() → list[Update]
│   ├── docker.py
│   ├── homeassistant.py
│   ├── fedora.py
│   └── github_watched.py
│
├── outputs/            One file per channel. send(item) → None (best-effort)
│   ├── notion.py         persistent, ignores parity gate
│   ├── telegram.py       push, is_push = True
│   ├── discord.py        push, is_push = True
│   ├── ntfy.py           push, is_push = True
│   └── gotify.py         push, is_push = True
│
├── curator/            Writes per-container + system Markdown notes
│   ├── core.py           Curator class (snapshot → prompt → write)
│   ├── system.py         `notes/system.md` from host probes
│   ├── incremental.py    "Append a line on hold/breaking" hook
│   ├── prompts.py        curator's prompt template (different from llm.py's)
│   └── helpers.py        env redaction, footer regex, truncation
│
├── prompts/            Built-in analyzer prompt template (Markdown)
│   ├── __init__.py       loader + LRU cache
│   └── analyzer.md       the rule set the LLM sees
│
├── web/                FastAPI + Jinja2 (no SPA, HTMX optional)
│   ├── __init__.py       create_app + middleware order
│   ├── lifecycle.py      scheduler hook (engine + digest crons)
│   ├── routes_updates.py
│   ├── routes_notes.py
│   ├── routes_interview.py
│   ├── routes_diagnostics.py  "What HomelabSage sees" page
│   ├── routes_settings*.py    schema-driven config UI + connection tests
│   ├── routes_wizard.py       first-run 3-step wizard
│   ├── routes_llm_profiles.py multi-profile switcher
│   ├── routes_health.py       /healthz
│   ├── auth.py                HTTP Basic Auth middleware
│   └── csrf.py                origin-check middleware
│
├── engine.py           run_once loop + plugin/output factories
├── llm.py              OpenAI-compat + Ollama client + tolerant JSON parser
├── models.py           Update / AnalyzedUpdate / Analysis / Severity
├── notes.py            NotesProvider (matches sections against subject)
├── github.py           latest_release + repo_metadata + classify_repo_health
├── images.py           find_alternatives (Docker Hub + LSIO cross-reference)
├── registries.py       Docker Hub anonymous v2 API helper (+ FloatingTagInfo)
├── cve.py              trivy / grype adapter (subprocess + JSON parse)
├── compose.py          docker-compose.yml dependency graph (mtime cache)
├── image_size.py       image-size growth detector (Docker Hub manifest size)
├── parity.py           parity-check probe (/proc/mdstat + mdcmd status)
├── diagnostics.py      "What I see" probe (per-container verdict)
├── csi.py              post-mortem: logs since last update + LLM diagnosis
├── analyse_url.py      one-shot URL analyser (GitHub/Codeberg)
├── digest.py           weekly rollup + per-channel dispatch
├── enrichment.py       fetchers for README, Docker Hub, container logs
├── redact.py           Sanitiser (IPs, hostnames, credentials)
├── scripts.py          cron / systemd timers / Unraid User Scripts walker
├── config_overlay.py   deep_merge for config.user.yaml on top of config.yaml
└── _time.py            datetime.now(UTC) helper + tz-aware ISO parser
```

## Key invariants

1. **Stable IDs.** `Update.id = f"{source}:{subject}:{new_version}"`. Re-detecting
   the same update is idempotent — `db.upsert()` replaces rather than duplicates.
   Tests pin this; new sources MUST follow the convention.

2. **Best-effort probes.** Every probe that touches the host or the network
   (CVE scan, parity check, image-size lookup, system curator probes) returns
   `None` on failure. The scan loop never throws because a plugin had a bad
   day — the analyzer sees whatever context was successfully gathered.

3. **Persistent vs push outputs.** `Output.is_push: bool` decides whether the
   parity gate silences the output during a parity check. Notion is persistent
   (`is_push = False`); all four push channels carry `is_push = True`. The
   weekly digest is its own thing — see `docs/output-protocol.md`.

4. **Settings are layered.** `config.yaml` is the deploy default; the web
   UI writes `config.user.yaml` overlay on top. Reads always merge:
   `Config(deploy)` → deep_merge with overlay → expand `${VAR}` → final.
   Tests use the same path; no special-casing for env vs disk.

5. **Schema-driven UI.** The settings page generates forms from
   `Cls.model_json_schema()`. Adding a new config field = add to the
   Pydantic model. No template edits.

6. **Async hygiene.** Plugins and engine run inside an asyncio event loop.
   Any blocking call (`subprocess.run`, `Path.rglob`, docker SDK methods)
   is wrapped in `asyncio.to_thread`. Search the codebase for the pattern
   if you add a new source — there's a regression hazard otherwise.

7. **No mutable globals.** Each `run_once` re-builds plugins and outputs
   from the current config. The scheduler hot-reloads YAML on every
   firing. The LLM profile switcher works the same way.

## How a scan flows (sequence)

```
scheduler  →  Engine.run_once()
              │
              ├─ probe parity gate (one call/run)
              ├─ if gate clear: flush pending_dispatches
              │
              └─ for each plugin:
                  │
                  ├─ plugin.scan() → list[Update]
                  │
                  └─ for each Update:
                      │
                      ├─ skip if (source,subject,new_version) already analyzed
                      ├─ NotesProvider.context_for(update.subject)
                      ├─ LLMClient.analyze(update, notes) → Analysis
                      ├─ db.upsert(AnalyzedUpdate)
                      ├─ curator.incremental.append_update_to_note (if risky)
                      │
                      └─ for each output:
                          │
                          ├─ if push_gated and output.is_push:
                          │      db.queue_pending_dispatch(id, output_id)
                          │      continue
                          │
                          └─ output.send(item)
              │
              └─ heartbeat ping (if configured)
```

## Where to add things

- **New source plugin**: drop a file in `plugins/`, subclass `Plugin`,
  implement `async def scan(self) -> list[Update]`. Wire in
  `engine.build_plugins()` and add a Pydantic config submodel under
  `config/sources.py`. The settings UI picks it up automatically once
  you add the block to `SETTING_BLOCKS` in `web/routes_settings.py`.
- **New notification channel**: drop a file in `outputs/`, subclass
  `Output`, set `is_push` appropriately, implement `async def send`.
  Wire in `engine.build_outputs()` and add a config submodel under
  `config/outputs.py`. See `docs/output-protocol.md`.
- **New analyzer signal**: add a context field on the `Update` (a plugin
  populates it) and a rule in `src/homelabsage/prompts/analyzer.md` that
  tells the LLM what to do when the context contains your field.
- **New CLI command**: drop a file in `cli/`, import `app` from
  `cli/_common.py`, decorate with `@app.command()`. Re-export from
  `cli/__init__.py`.
