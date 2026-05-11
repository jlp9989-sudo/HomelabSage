# HomelabSage

[![CI](https://github.com/jlp9989-sudo/HomelabSage/actions/workflows/ci.yml/badge.svg)](https://github.com/jlp9989-sudo/HomelabSage/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-pre--alpha-orange)](#)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)

**AI-powered homelab analyzer, update tracker and improvement advisor.**

Watches your stack (Docker containers, Home Assistant, Linux packages, firmware, news feeds, RSS) and uses a **local LLM** to tell you, for each update:

- Whether there are **breaking changes** that affect *your* current config.
- Whether parts of your **setup are obsolete** because the new version brings them built-in.
- Whether there are **new features relevant to your homelab**.
- A short, structured summary so you don't have to read raw release notes.

The LLM doesn't analyze updates in a vacuum — you can point it at your own `notes/` directory (markdown), and it pulls in only the sections that match the update subject. That's how it knows "your Elasticsearch is versionlocked on 8.x because of RAGFlow" before recommending an upgrade.

> Status: **pre-alpha**, in active development. The Docker plugin is the most mature; the others are scaffolds.

---

## Why

Most release-note watchers (Diun, WatchTower, Renovate) tell you *that* there is a new version. None of them read the changelog *and* your own constraints.

HomelabSage is the missing layer:

```
   ┌──────────────────────────────────────────────────────────────┐
   │  Plugins         scan() → list[Update]                       │
   │  ────────                                                    │
   │   docker         containers → OCI label → GitHub releases    │
   │   homeassistant  /api/config + sensor.hacs                   │
   │   fedora         dnf check-update                  (planned) │
   │   llamacpp       releases.atom                     (planned) │
   │   huggingface    repo revisions                    (planned) │
   │   rss_feeds      announcements / forums            (planned) │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  Engine          for each Update:                            │
   │  ──────                                                      │
   │    1. fetch release notes (markdown)                         │
   │    2. NotesProvider — pull matching sections from your docs  │
   │    3. LLM analyze (Ollama / llama.cpp / OpenAI / Anthropic)  │
   │    4. persist (SQLite)                                       │
   │    5. route to outputs                                       │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  Outputs                                                     │
   │  ───────                                                     │
   │   Web UI       FastAPI + Jinja2 (HTTP Basic Auth optional)   │
   │   Notion       database row per analyzed update              │
   │   Telegram     severity-gated push                           │
   │   Heartbeat    Uptime Kuma / Healthchecks ping after each run│
   └──────────────────────────────────────────────────────────────┘
```

Each layer is one file. The whole thing is ~1,900 lines of Python.

---

## Features

- **Plugin-based sources.** One file = one source. See [docs/plugin-sdk.md](docs/plugin-sdk.md).
- **Local LLM by default.** Ollama-compatible API; works with [Ollama](https://ollama.com), [llama.cpp server](https://github.com/ggml-org/llama.cpp), LM Studio, or any OpenAI-compat endpoint. Falls back to OpenAI / Anthropic if you really want to.
- **Tolerant JSON parser.** Strips markdown fences, surrounding prose, accepts case-insensitive severity, falls back to a `summary`-only best-effort when the model bends the schema.
- **Your notes are the secret sauce.** Point `notes.notes_dir` at a folder of `.md` files (your CLAUDE.md, ARCHITECTURE.md, OPS.md, etc). For each update, only the sections that mention the subject get injected — no token bloat.
- **Web UI with editor.** Read the analyzed list and edit notes from the browser (HTMX-style, no SPA). HTTP Basic Auth optional but recommended.
- **Path-traversal safe.** Notes editor refuses `..` and non-`.md`/`.txt` extensions.
- **Stable IDs.** `source:subject:new_version` — re-running a scan doesn't create duplicates.
- **Heartbeat-friendly.** Pings an Uptime Kuma push monitor (or any URL) after each successful scan.
- **Outputs are pluggable too.** Notion + Telegram today; webhooks/email easy to add.

---

## Install

### Docker Compose (recommended)

```yaml
services:
  homelabsage:
    image: ghcr.io/jlp9989-sudo/homelabsage:latest
    container_name: homelabsage
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./config.yaml:/app/config.yaml:ro
      - ./.env:/app/.env:ro
      - ./notes:/app/notes        # optional — your markdown notes
      - homelabsage-data:/data
    environment:
      TZ: Europe/Madrid

volumes:
  homelabsage-data:
```

Then:

```bash
cp config.example.yaml config.yaml      # edit
cp .env.example .env                    # edit (LLM endpoint, tokens)
docker compose up -d
```

Open `http://<host>:8000` (or wherever you bound it).

### Unraid

There's an Unraid Community Apps template under `unraid/` (planned). Until then, the Docker Compose route above works fine via [Dockge](https://github.com/louislam/dockge) or `User Scripts`.

### From source

```bash
git clone https://github.com/jlp9989-sudo/HomelabSage
cd HomelabSage
pip install -e ".[dev]"

cp config.example.yaml config.yaml      # edit
homelabsage check                       # one-shot scan
homelabsage serve                       # web UI + scheduler
```

Python ≥ 3.11.

---

## Configuration

`config.yaml` is the only required file. The example is heavily commented — read it. Highlights:

```yaml
llm:
  provider: ollama                       # ollama | openai | anthropic | disabled
  endpoint: http://localhost:11434       # or your llama.cpp / LM Studio endpoint
  model: qwen3:30b                       # ≥30B parameters recommended for analysis
  context_size: 32768
  api_key: "${LLM_API_KEY:-}"            # only for openai/anthropic
  timeout: 180

sources:
  docker:
    enabled: true
    socket: /var/run/docker.sock
    overrides: {}                        # container_name → github owner/repo
    skip:
      - "^.*_(redis|valkey|postgres|mysql|mariadb|db)$"

  homeassistant:
    enabled: false
    url: http://homeassistant.local:8123
    token: "${HA_TOKEN:-}"
    include_hacs: true
    include_addons: true

outputs:
  notion:
    enabled: false
    api_key: "${NOTION_API_KEY:-}"
    database_id: "${NOTION_DB_INFRA_UPDATES:-}"
    write_policy: always                 # always | only_action_required
  telegram:
    enabled: false
    bot_token: "${TELEGRAM_BOT_TOKEN:-}"
    chat_id: "${TELEGRAM_CHAT_ID:-}"
    min_severity: high                   # critical | high | medium | info

scheduler:
  enabled: true
  cron: "0 9 * * *"
  timezone: Europe/Madrid
  heartbeat_url: "${HEARTBEAT_URL:-}"

web:
  enabled: true
  host: 0.0.0.0
  port: 8000
  auth:
    enabled: false                       # recommended: true if not loopback
    username: admin
    password: "${HOMELABSAGE_PASSWORD:-}"

notes:
  notes_dir: ./notes                     # folder of *.md files
  extra_docs: []                         # always-included (keep short)
  max_chars: 4000
```

Any `${VAR}` is expanded from the environment (or a `.env` file next to `config.yaml`). `${VAR:-default}` works.

### LLM provider setup

**llama.cpp server / Ollama (local, recommended):**

```yaml
llm:
  provider: ollama
  endpoint: http://192.168.1.10:11434
  model: qwen3:30b
```

The "ollama" provider hits the OpenAI-compatible `/v1/chat/completions` endpoint, so any server that speaks that protocol works (Ollama, llama.cpp `llama-server`, LM Studio, vLLM, Text Generation WebUI). The name is historical — don't read into it.

**OpenAI / Anthropic (cloud, fallback):**

```yaml
llm:
  provider: openai
  endpoint: https://api.openai.com
  model: gpt-4o-mini
  api_key: "${LLM_API_KEY}"
```

**Free cloud LLM tiers (OpenAI-compatible):**

No GPU at home? All three providers below expose an OpenAI-compatible Chat Completions API — only `endpoint`, `model` and `api_key` change. The default `0 9 * * *` scan cadence fits inside every free tier listed.

*Groq* — fastest free inference, generous daily quota:

```yaml
llm:
  provider: openai
  endpoint: https://api.groq.com/openai
  model: llama-3.3-70b-versatile     # check console.groq.com/docs/models for current ids
  api_key: "${LLM_API_KEY}"
```

*OpenRouter* — single account, 200+ models including free variants:

```yaml
llm:
  provider: openai
  endpoint: https://openrouter.ai/api
  model: meta-llama/llama-3.3-70b-instruct:free
  api_key: "${LLM_API_KEY}"
```

*Google Gemini* — Flash free tier (~1,500 req/day):

```yaml
llm:
  provider: openai
  endpoint: https://generativelanguage.googleapis.com/v1beta/openai
  model: gemini-2.0-flash
  api_key: "${LLM_API_KEY}"
```

> Tip: run `homelabsage check -v` once after switching to confirm the JSON schema parser is happy. Free models occasionally bend the schema; the tolerant parser handles most cases but `-v` will surface anything it had to fall back on.

**Disabled (no LLM, detection only):**

```yaml
llm:
  provider: disabled
```

Updates are still detected and stored. No `summary`, no `breaking_changes`, no `recommended_action`. Useful for first-run sanity checks.

---

## CLI

```bash
homelabsage check                # one-shot: scan → analyze → output
homelabsage list                 # show stored updates
homelabsage list --source docker --status new --limit 20
homelabsage serve                # web UI + scheduler (long-running)
homelabsage version
```

Add `-v` for debug logging, `-c /path/to/config.yaml` for a custom config.

---

## Web UI

`/`              dashboard of analyzed updates, severity-coloured
`/notes`         list your markdown notes
`/notes/edit/<file>`  edit a note in-browser
`/healthz`       liveness (always 200, no auth — for healthchecks)

Auth is HTTP Basic. `/healthz` is excluded so container/Kuma probes keep working.

---

## Plugin SDK

Adding a new source is a single Python file. See [docs/plugin-sdk.md](docs/plugin-sdk.md) for a full walkthrough.

Short version:

```python
from homelabsage.plugins import Plugin
from homelabsage.models import Update

class MyPlugin(Plugin):
    id = "myplugin"

    async def scan(self) -> list[Update]:
        return [
            Update(
                source=self.id,
                subject="thing-i-watch",
                current_version="1.0.0",
                new_version="1.1.0",
                release_url="https://...",
                release_notes="...changelog body...",
                context={"any": "extra fields for the LLM"},
            )
        ]
```

The core handles LLM analysis, dedup by stable id, persistence, and routing to outputs. Plugins only emit `Update` items.

---

## Development

```bash
git clone https://github.com/jlp9989-sudo/HomelabSage
cd HomelabSage
pip install -e ".[dev]"

pytest -q                # 35 tests, <1s
ruff check .             # lint
mypy src/homelabsage     # types
```

CI runs the same three checks on every PR — see `.github/workflows/ci.yml`.

### Project layout

```
src/homelabsage/
  __init__.py
  cli.py            Typer entrypoint
  config.py         YAML + env-var loader (Pydantic)
  db.py             SQLite, stdlib only
  engine.py         scan → LLM → persist → outputs
  github.py         tiny GitHub API helper
  llm.py            OpenAI-compat client + tolerant JSON parser
  models.py         Update / Analysis / Severity / Status
  notes.py          NotesProvider + NotesEditor
  web.py            FastAPI app + Basic Auth
  outputs/          notion.py · telegram.py · heartbeat.py
  plugins/          docker.py · homeassistant.py · …
  templates/        Jinja2 (server-rendered, no JS framework)
tests/              pytest, 35 tests
```

---

## Known limitations

- No login UI — HTTP Basic Auth only. Put it behind Cloudflare Access / Authelia / Tailscale if you expose it publicly.
- Polls GitHub at scan time. Set `GITHUB_TOKEN` in `.env` to lift the 60-req/hour anonymous rate limit.
- Tag-comparison is semver-only. Variant tags (`alpine`, `cuda`, `openvino`, `latest`) are explicitly *not* compared — they're skipped rather than risk false positives.
- The `homeassistant` plugin needs a long-lived access token. HACS detection depends on the [HACS sensor](https://hacs.xyz/) being exposed.

---

## License

MIT — see [LICENSE](LICENSE).
