# UI refactor + ROADMAP decisions — session 2026-05-12

Not a polished design doc. Notes captured during the session so the next one can pick up.

---

## Honest answer on the UI refactor

**Recommendation: do not refactor the UI yet. Add the four small UI-shaped features first, watch what users actually touch, then refactor against real usage.**

The current web layer is 172 lines of FastAPI + 180 lines of Jinja templates + zero JS/CSS files. It does what it does — list updates, edit notes, basic auth — and the architecture is already extensible *for the engine* (plugins are one file, outputs are one file). The bottleneck for "easy for non-technical users" is not the templates; it's that **every behaviour that matters lives in `config.yaml` and `.env`**. A SPA pointed at the same backend would expose the same problem.

What would actually move the needle:

1. **Settings backend, not settings UI yet.** Carve a thin `/api/settings` layer that reads + writes the same Pydantic config we already have. Persistence target: a new `config.user.yaml` written next to the existing `config.yaml` (config.yaml stays the deploy default, user overrides win). One endpoint per logical block (`/api/settings/llm`, `/api/settings/outputs/notion`), each backed by the same Pydantic model that today validates the file. *No JS framework needed for this — Jinja + HTMX is fine.*
2. **One-file-per-page convention.** Today `web.py` mixes routes for `/`, `/notes`, `/run`, `/healthz`, and basic-auth middleware. Split into `web/routes_updates.py`, `web/routes_notes.py`, `web/routes_settings.py`, `web/routes_interview.py` so adding a feature is "drop a new routes file + a template" instead of "merge into a 200-line module."
3. **Add HTMX, do not add React.** Updates list, model selector, interview answer form — all are stateless POST → re-render. HTMX is a script tag, no build step, no node_modules in a Python container. A real SPA buys nothing here and triples maintenance cost.
4. **First-run wizard.** New `/setup` page that's shown when `config.user.yaml` doesn't exist yet. Three steps: pick LLM (test connection live), enable Docker/HA plugins, optional outputs (Notion/Telegram). End state: writes `config.user.yaml`, drops a marker file, future visits go straight to `/`.

**Things I would add that the prompt didn't mention:**

- **Plugin enable/disable from the UI.** The plugin and output system is already file-per-thing; surfacing on/off toggles is trivial and is the second-most-asked thing after model selection (judging from how readme installs flow).
- **Connection test buttons.** "Test LLM endpoint", "Test Notion DB", "Test Telegram chat id" — each just calls the existing client with a tiny no-op. Saves the user a debugging round-trip when they paste a bad key.
- **Schema-driven forms.** The Pydantic models are already the source of truth. Render forms from `model.model_json_schema()` instead of hand-writing each input — adding a config field becomes "add to model, no template change." This is what makes the UI extensible *without touching much code each time*, which was the user's first goal.
- **Read-only "what I see" page.** A single page that prints what HomelabSage detected about the host: which containers, which were skipped (and why — `skip:` regex matches), which have `repo:` resolved, which would be curated. Hugely useful for first-time setup and zero engine changes.

### What I would NOT do

- Rewrite in React/Vue. The current templates render in 180 lines total. A SPA + API split costs at least 800 lines of TypeScript before it does anything new, and the user does not need offline-first / real-time / mobile-app behaviour.
- Add a database for user-edited settings. The Pydantic config + YAML file is the right shape — diffable, restorable, version-controllable. A settings table would just be a worse YAML.
- Add login UI (username + password DB). HTTP Basic + reverse proxy is fine for a self-hosted single-user tool. Multi-user auth is out of scope until a real second user appears.

### Sequencing (proposal for ROADMAP v0.5)

1. Split `web.py` into a `web/` package (mechanical, 1 hour, zero behaviour change).
2. `/api/settings/*` read endpoints (returns current YAML-merged config).
3. `/api/settings/*` write endpoints + `config.user.yaml` overlay.
4. Schema-driven settings page (one HTMX form per Pydantic submodel).
5. Connection-test buttons.
6. First-run wizard.
7. Interview mode (depends on backend feature — see ROADMAP).

The first two are pure refactor / scaffolding. Stop after step 2 and reassess if anyone's actually asking for the wizard.

---

## Feature evaluations (the four asks)

### 1. Alternative image detector

> "Buscar si existe una imagen alternativa mejor mantenida o más compatible (Docker Hub, LSIO catalog, GitHub)."

**Verdict: v0.3.x committed.** Has a concrete, deterministic signal — image popularity / last_pushed / `linuxserver/` namespace — and it composes naturally with the existing GitHub helper. The hard part is honest scoring, not implementation.

**Approach:**
- New helper `homelabsage.images.find_alternatives(image: str) -> list[Alternative]`.
- Sources, in increasing trust:
  1. Hub search API (`hub.docker.com/v2/search/repositories?query=<base>`). Cheap, broad, noisy.
  2. LSIO catalog (`fleet.linuxserver.io/api/v1/images`). Curated, narrow.
  3. GitHub code search for `FROM <image>` in popular compose files. Slow, last resort.
- An "alternative" is only emitted if it satisfies all of: same primary purpose (heuristic on Hub description embedding similarity OR explicit upstream link), last push within 90 days, ≥10× the pull count of the current image, *and* a stable tag matching the current semver shape.
- Surfaces in the analyzer as one extra `context` field, the LLM gets to decide whether to mention it. **No hard recommendation from the heuristic alone** — same honesty bar as the curator rules.

**Cost:** 1–2 days. Mostly the cross-source dedup logic.

### 2. New software discovery

> "Cruzar las notas del usuario con fuentes externas (awesome-selfhosted, GitHub trending, r/selfhosted) para detectar software nuevo que cubra mejor su caso de uso. Sin alucinaciones, solo con fuente verificable."

**Verdict: backlog. Right shape, wrong moment.** Honest implementation requires either embeddings over awesome-selfhosted (new dependency surface) or per-category curated lists (manual maintenance forever). Both are real work and the value depends on the curator having already written good notes per service — which is v0.4. Build v0.4 first, see whether the curator-produced notes are rich enough to drive matching, then revisit.

**If we do it, the safe shape is:**
- Crawl awesome-selfhosted once a week, store as a categorised dataset.
- For each existing note, the LLM extracts a `category + primary_function` tuple. Cached.
- Match function to function; only emit a suggestion when (a) the alternative is in the same category, (b) the alternative has more recent activity than the user's installed image, (c) the LLM can write a one-line reason citing both inputs verbatim.
- Anything that can't satisfy (c) is dropped, not invented.

**Cost:** 1 week minimum. Park until v0.4.x lands.

### 3. Unraid User Scripts + cron detection

> "Analizarlos igual que los containers."

**Verdict: v0.2.x committed.** This is genuinely easy and high-value for Unraid users (which is half the audience). Two plugins:

- `plugins/unraid_user_scripts.py`: walk `/boot/config/plugins/user.scripts/scripts/` (each subdir = one script, with `script` + `name` + `description` files). Emit one `ContainerSnapshot`-equivalent per script. Reuse the curator's prompt with a different placeholder set (`script_purpose_hint`, `script_body`, `schedule`).
- `plugins/cron.py`: parse `/etc/cron.d/*` plus user crontabs. Same shape.

**Architecture impact:** the curator currently assumes a Docker container as its unit-of-work. Generalise `ContainerSnapshot` → `CuratableTarget` (just rename; the four fields generalise cleanly — `name`, `kind`, `discovery_source`, `payload`). Plugins emit targets of `kind="container"|"user_script"|"cron"`, the curator dispatches to the right prompt template based on kind.

**Watch out for:**
- User Scripts paths are inside `/boot` which is FAT — host-side `nsenter` access required from inside the container (the project's CLAUDE.md trap #1). Document this; provide a `homelab.unraid_path` config knob so non-Unraid hosts don't trip.
- Cron entries that exec `docker exec <container>` should cross-reference the container's note (don't duplicate analysis). Cheap: dedup by extracted container name.

**Cost:** 2 days, mostly polish + the `CuratableTarget` rename.

### 4. Interview mode in GUI

> "Cuando HomelabSage no entienda el propósito de algo, mostrar pregunta en la web UI; la respuesta se guarda en notes/."

**Verdict: v0.4.x committed — but as the discovery vehicle for self-curating notes, not a side feature.** This is what makes v0.4 actually work for non-technical users. Without it, v0.4 produces "(no purpose stated yet — fill in)" notes that nobody fills in. With it, every such note becomes a one-question prompt the next time the user opens the UI.

**Backend design (the simple piece):**
- New SQLite table `interview_questions`: `id, target_kind, target_id, question_text, status (open|answered|dismissed), created_at, answered_at, answer_text`.
- The curator's existing rule 7 (the "(no purpose stated yet)" fallback) becomes the trigger — instead of writing the fallback line, the curator emits an `InterviewQuestion` row. The note is written with the placeholder; when the user answers, the note is regenerated with the answer injected as an additional input.
- One question at a time per target. Re-running curate against the same target doesn't duplicate the question.

**Frontend (the piece that's blocked on the UI refactor above):**
- A `/interview` route, or — better — an unobtrusive banner at the top of the dashboard: *"3 services I don't fully understand. Answer in 30s →"*. Click expands inline (HTMX), one question at a time, free-text answer. Submit writes to SQLite, regenerates the affected note in the background.

**Why this is the v0.4 critical path:** the entire v0.4 sales pitch is "the AI writes your notes for you so the analyzer gets sharper without manual work." If 30 % of services end up with the placeholder line, the pitch falls flat. Interview mode is what closes that gap — and it generalises (script, cron, HA automation, whatever the next plugin emits).

**Cost:** 3 days, but two of them are the UI refactor sequencing above (steps 1–3 are blockers).

---

## Where to land each in the ROADMAP

- **Alternative image detector** → new v0.3.x bullet, alongside "Abandonware radar" (same shape — adds context, doesn't dictate).
- **User Scripts / cron detection** → new v0.2.x bullet (`CuratableTarget` rename + two plugins).
- **Interview mode** → new v0.4.x subsection, scoped as "curator UX, not a side feature."
- **New software discovery** → backlog 🤔 with reasoning (depends on v0.4 producing categorisable notes).
- **UI refactor (this doc)** → new v0.5 section titled "UI extensibility + non-technical onboarding." Steps 1–4 there; first two are blockers for interview mode and for the settings UI.

---

## What's missing from the user's prompt that I'd add

1. **Settings export / import.** Once the UI writes `config.user.yaml`, expose a "download my config" + "load config" pair. Reduces fear of clicking around — they can always re-import a known-good blob.
2. **Run history view.** SQLite already stores update analyses; we don't surface "which scan run produced this row." A `/runs` page (one row per scheduled run with counts + duration) is one query + one template.
3. **Per-target dry-run from the UI.** The curator has `--dry-run --target X`; the UI should expose "preview note for this container" so the user can read what would be written before approving. Closes the loop on the curator's honesty guarantees in a way the user can see.
