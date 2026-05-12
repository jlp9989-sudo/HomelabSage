# Roadmap

Living document. Items move down (toward "done") or out (toward "rejected, here's why") as we learn.

Legend: **✅ committed** · **⚠️ committed with caveats** · **🤔 backlog** · **❌ rejected**

---

## v0.1.0 — Public release (immediate)

The code is feature-complete for a first release; these are publication chores.

- ✅ **Tag `v0.1.0` and let the Docker workflow publish to GHCR.** The image reference in `README.md` (`ghcr.io/jlp9989-sudo/homelabsage:latest`) currently 404s.
- ✅ **GitHub topics:** `homelab`, `self-hosted`, `docker`, `ollama`, `homeassistant`, `release-notes`, `llm`. Discoverability for free.
- ✅ **README → "Free cloud LLM options" section.** Groq, OpenRouter, Google Gemini are all OpenAI-compatible — `endpoint` + `api_key` swap, no code change. Lowers the barrier for users without a local GPU.
- ✅ **Surface `GITHUB_TOKEN` rate-limit story in README.** It's already in `.env.example` but not visible in the install flow. Add a one-liner in *Configuration* explaining 60/h → 5000/h, and link to fine-grained PAT creation.

---

## v0.2.x — Detector layer (short-term, high-value low-cost)

All of these are additive: they enrich the `Update` payload that the LLM already sees, so the analyzer gets sharper without a prompt rewrite.

- ✅ **Port-conflict detector.** Diff `EXPOSE` of the candidate image against current container labels and any Traefik/Nginx config files declared in `notes/`. Single `docker manifest inspect` per image.
- ✅ **New-volume detector.** Compare `VOLUME` declarations old vs new. Flag any new path that isn't already mounted — the classic data-loss-on-update trap.
- ✅ **Network-capability diff.** `cap_add` / `privileged` / `network_mode` changes between versions. Tiny addition, large security win. Folds into the CVE block below.
- ✅ **CVE adapter (Trivy/Grype output → LLM context).** Don't reimplement vulnerability scanning. Run `trivy image` (optional, only if binary present) and feed the JSON into the analyzer's prompt as one more `context` field. Single-purpose plugins are the project's discipline.
- ✅ **Weekly digest output.** Sunday 09:00 cron entry that posts a single Telegram/email message: counts, top severities, abandoned containers (see below). The scheduler already exists — this is one new output module.
- ✅ **Webhook-style outputs: Discord / Ntfy / Gotify.** Three of the four most-asked notification channels on r/selfhosted (the fourth is Telegram, already shipped). All three share the same shape — a small `Output` class that POSTs JSON to a configured URL, no auth juggling, no SDK dependency. Concretely:
   * **Discord** — `POST <webhook_url>` with `{"content": "...", "embeds": [{...}]}`. Per-severity colour mapping on the embed (`0xc0392b` red for critical, `0xe67e22` orange for high, …). Webhook URL is the only knob; no app registration required.
   * **Ntfy** — `POST <topic_url>` (e.g. `https://ntfy.sh/my-topic`) with a plain-text body + `Title:`, `Tags:`, `Priority:` headers. Self-host-friendly default (every homelabber knows about ntfy.sh or runs their own).
   * **Gotify** — `POST <server>/message?token=<token>` with `{"title":..., "message":..., "priority":N}`. Same shape as Telegram structurally, just a different endpoint and a numeric priority instead of a severity enum.
   Each one is ~80 lines of code + tests + one config submodel + a settings UI form (the schema-driven renderer already handles strings + enums, so no template work). Estimated 1 day for all three combined. Architecturally identical to the existing Telegram output — model on that.
- ✅ **Orphan-image detector.** Containers `Status=exited` for >30 days with active updates pending. Surfaces "I forgot this existed and it's full of CVEs" cases.
- ✅ **Parity-aware notification gate.** On Unraid, check `/proc/mdstat` (or `mdcmd status` parsed) before pushing notifications. If a parity check / disk rebuild is running, queue notifications for after. Specific to Unraid users but trivial.
- ⚠️ **Renamed/deprecated env-var detector.** Doable, but requires the LLM to parse the changelog rather than a deterministic diff (env vars aren't declared anywhere machine-readable). Implement as a **prompt rule** in `engine.py` ("if the changelog mentions env-var renames, list them as a `breaking_changes` entry"), not a separate detector. Cheaper, equally effective.
- ⚠️ **Silent DB migration warning.** Same approach: prompt rule, not detector. Add to the LLM system prompt: *"If the release notes mention schema migration, ALTER TABLE, or one-shot data backfills, set `recommended_action` to a step that includes 'do not interrupt the first start after upgrade'."* Zero new code, big behavioural change.
- ✅ **Unraid User Scripts + cron detection.** Two new plugins: `unraid_user_scripts` walks `/boot/config/plugins/user.scripts/scripts/` (each subdir = one script with `script` + `name` + `description` files), `cron` parses `/etc/cron.d/*` plus user crontabs. Both emit `CuratableTarget` items (rename of the curator's current `ContainerSnapshot` — the four existing fields generalise: `name`, `kind`, `discovery_source`, `payload`). Curator dispatches to a kind-specific prompt template. Cron entries that exec `docker exec <container>` cross-reference the container's note so we don't duplicate. Trap: `/boot` is FAT and host-side only — needs `nsenter` access from inside the container or an `unraid_path` host knob for non-Unraid setups. ~2 days; high value for the Unraid half of the audience.
- ✅ **`<think>` block stripper in the curator response handler.** Some OpenAI-compat servers (Groq's `qwen/qwen3-32b`, Deepseek-R1, future reasoning models) return the chain-of-thought inline in `content` instead of an out-of-band `reasoning_content` field. Without sanitisation we write the entire `<think>...</think>` to `notes/<service>.md`. Strip on the way out of `LLMClient.generate_text`; one regex, big quality win for cloud users. Surfaced in the 2026-05-12 benchmark.

---

## v0.3.x — Cross-cutting features (medium-term)

Bigger changes. Each one is a multi-day chunk.

- ✅ **Watched repos plugin.** New source (`plugins/github_watched.py`) that analyzes GitHub repos the user explicitly marks for tracking, the same way the docker plugin analyzes running containers. Expands HomelabSage's scope beyond Docker: toolboxes (e.g. `kyuz0/amd-strix-halo-toolboxes`), scripts, dotfiles, firmware repos, anything the user runs in production that isn't a container. Authentication via `GITHUB_TOKEN` (already in `.env.example`); the plugin reuses the existing `homelabsage.github.latest_release` and `repo_metadata` helpers, so the actual scan logic is ~30 lines. The interesting part is **scoping**: we deliberately do NOT auto-import the user's full `starred` or `watching` list (too noisy — most people star things they'll never run). Instead, the user opts in per-repo. Storage: a new `watched_repos` table (owner/name + nickname + active flag + added_at). Management surface lives in the v0.5 UI (`/watched` page: add by `owner/name`, toggle active/inactive, delete) so non-technical users can curate without editing YAML. CLI access via `homelabsage watched add|list|remove|toggle` for power users. Same prompt rules that today fire on `repo_health` apply here without changes — the analyzer already knows what to do with a stale/abandoned repo. ~2 days for the plugin + CLI; the UI piece slots into v0.5 step 7 alongside interview-mode.

- ✅ **CSI mode (post-mortem assistant).** New CLI subcommand `homelabsage csi <container>` that:
  1. Greps SQLite for the latest update applied to that container,
  2. Pulls `docker logs --since=<that timestamp>`,
  3. Filters to ERROR/WARN/FATAL lines,
  4. Sends `{update_diff, log_excerpt, your_notes}` to the LLM.
  The autonomous variant (run nightly on every `exited` container) is the same code in a scheduler hook.
- ✅ **LLM routing by severity.** Heuristic on the changelog before full analysis (regex for `BREAKING`, semver bump magnitude). Patch versions → small/cheap model (Gemini Flash-Lite, free tier). Major versions → the heavy local model. Keeps the Halo free for other workloads and uses the free Gemini tier where it fits. Routing config in `llm:` block.
- ✅ **Abandonware radar.** GitHub API call per source repo: `last_commit_date`, `open_issues_count`, `archived` flag. Annotate each `Update` with `repo_health: {alive | stale | abandoned}`. Suggesting forks is harder (no clean signal); skip that part for v0.3, just flag the state.
- ✅ **Dependency-cascade detector (compose-aware).** Parse `depends_on` and shared networks from `docker-compose.yml` files in a configurable scan path. When container A has a major update, list every B that depends on A in the analysis output. Most useful for Dockge users with many small stacks.
- ✅ **HACS cascade for HA Python bumps.** Specific case of the above. When `homeassistant.core` releases, fetch the new `homeassistant/package_constraints.txt`, diff Python version. If bumped, query GitHub Issues across user's installed HACS repos for `python 3.x` mentions opened in the last 60 days. High value for HA-heavy setups.
- ✅ **Sanitised stack export.** `homelabsage export --redact` produces a single-file dump of compose configs + container env + recent analyses with: IPs → `10.0.0.X`, hostnames → `host-N`, anything matching `*_PASSWORD|*_TOKEN|*_KEY` → `<redacted>`. For pasting into GitHub issues. Cheap, big community-friendliness win.
- ✅ **Alternative-image detector.** New helper `homelabsage.images.find_alternatives(image)` that crosses three sources: Docker Hub search API (broad, noisy), the LSIO catalog `fleet.linuxserver.io/api/v1/images` (curated, narrow), and a GitHub code search for `FROM <image>` in popular compose files (slow, last resort). An "alternative" is only emitted when it satisfies ALL of: same primary purpose (Hub description similarity or explicit upstream link), last push within 90 days, ≥10× the pull count of the current image, and a stable tag matching the current semver shape. Surfaced to the analyzer as one extra `context` field — the LLM decides whether to mention it. No hard recommendation from the heuristic alone (same honesty bar as the curator rules). Composes cleanly with the existing GitHub helper. ~1–2 days, mostly cross-source dedup. Sibling of [Abandonware radar].

---

## v0.4 — Self-curating notes (flagship)

> The user's pitch: "the AI should write the per-system notes itself, knowing what to put in and what not to."

Today, `notes/` is fully manual. The "secret sauce" only works if the user remembers to write things like *"Elasticsearch versionlocked at 8.x because RAGFlow"*. Most won't. This makes that step automatic.

**Approach (sketch):**

- New plugin output kind: `NoteCurator`. Runs **after** the engine writes an `Analysis`, with full read+write access to `notes/`.
- Triggered in two cases:
  1. **Discovery pass** (one-shot, on demand): inspect every container's `docker inspect` + recent `docker logs` + compose file. Generate a per-service note with **only** non-obvious facts — the curator must skip anything trivially derivable from `docker inspect` (image name, ports, basic mounts). Keep it tight: one paragraph plus bullets.
  2. **Incremental update**: when a `recommended_action` is `hold` or `breaking_changes` is non-empty, append a one-liner to the relevant note (`<service>.md`) explaining *why* the update was held / what config it would have broken. So the next scan has a memory of past decisions.
- **Style rules** baked into the curator's system prompt:
  - Lead with the constraint, not the fact (*"Versionlocked at 8.x because RAGFlow needs the old auth mechanism"*, not *"Running version 8.x"*).
  - No timestamps inside the note body — use git history for that.
  - Never restate what `docker inspect` shows.
  - Mark each entry with a stable footer line like `<!-- curator: <update-id> -->` so future runs can dedupe.
- **Safety:** atomic writes, every change committed to `notes/.git` if it's a repo, daily diff posted to the weekly digest so the user can review what the curator wrote.

Pattern is borrowed from how Claude Code maintains its `MEMORY.md` — same shape, same discipline (only save what's non-obvious; lead with the *why*).

This is the most architecturally invasive item on the roadmap; spec it as a separate design doc before coding.

### System-level curator note (v0.4.1)

Sibling feature of the per-container curator. One auto-generated `notes/system.md` that captures the **host environment** the containers run inside, so every subsequent LLM call (analyzer and per-container curator) has shared baseline context without paying the token cost of re-discovering it for each update.

Contents to capture:

- Kernel: `uname -r`, distro + version (`/etc/os-release`).
- Docker daemon: version, storage driver, cgroup driver, default runtime (`docker info` JSON).
- Network topology: which bridges / macvlan / host-mode networks exist, presence of Tailscale / WireGuard, exposed reverse-proxy hostnames (Caddy / Traefik / Cloudflare Tunnel).
- Host resources: total RAM, CPU model, primary disk usage, GPU(s) and runtime (`nvidia` / `rocm` / `vulkan`), zpool list if ZFS.
- Unraid-specific (auto-detect): plugin list, array status (parity / pending check), share layout (cache pool vs array).
- Last regeneration timestamp, same `<!-- curator: system@<host-fingerprint> -->` footer pattern for dedup.

Implementation notes:

- New `Curator.curate_system()` that calls a separate prompt template (`curator.system_prompt_template_path` — falls back to a built-in default like the per-container one).
- Probes are best-effort and pluggable: each probe (`_probe_kernel`, `_probe_docker_info`, `_probe_unraid`) returns `None` on absence and the prompt builder simply omits the corresponding block. No probe should crash the run if its target tool is missing.
- The note matcher in `NotesProvider` already does substring matching on subject + keywords, so `system.md` will be injected on every analyzer call where the update touches kernel modules, network stack, or host resources — exactly the cases where it matters.
- CLI: `homelabsage curate --system` (one-shot). Footer fingerprint stable across reruns unless the host actually changes, so re-runs are idempotent.

Out of scope for this iteration: distributing one `system.md` across multiple hosts (e.g. a Tower + a separate Halo GPU box). When that becomes useful, split into `system-<hostname>.md` files.

### Interview mode (v0.4.2) — critical path for v0.4 to actually work

The curator's rule 7 (`(no purpose stated yet — fill in)`) is the honesty escape hatch. In practice, **30–40 % of containers in a typical homelab trip rule 7** (verified empirically on the 2026-05-12 benchmark — `tintes` and `FileBrowser-PNP` both lacked a purpose hint). If those placeholder notes never get filled in, v0.4's self-curating promise falls flat for non-technical users.

Interview mode closes the gap. Whenever the curator would emit the rule-7 fallback line, it instead opens an `InterviewQuestion` row in SQLite. The web UI surfaces these as one-question prompts ("`tintes` — what does this service do for you?"). The user answers in 30 seconds; the curator regenerates that service's note with the answer injected as an additional input.

**Backend (the simple piece):**

- New SQLite table `interview_questions`: `id, target_kind, target_id, question_text, status (open|answered|dismissed), created_at, answered_at, answer_text`.
- The curator detects the rule-7 fallback in its own output and writes an `InterviewQuestion` instead of (or alongside) the placeholder note.
- One question at a time per target — re-running curate against the same target doesn't duplicate the question.
- Generalises to any `CuratableTarget` kind — user scripts, cron jobs, anything where purpose is unclear.

**Frontend (blocked on the v0.5 UI refactor):**

- Unobtrusive banner at the top of the dashboard: *"3 services I don't fully understand. Answer in 30s →"*. Click expands inline (HTMX), one question per service, free-text input.
- Submit writes the answer to SQLite, schedules a background `curate --target X --force` so the note is regenerated.

**Why this is critical-path, not nice-to-have:** the entire v0.4 sales pitch is "the AI writes your notes for you so the analyzer gets sharper without manual work." Without interview mode, every service whose purpose isn't obvious from `docker inspect` ends up with a placeholder line that nobody fills in — and the analyzer's prompt gets a fraction of the context it would otherwise. With interview mode, the gap closes in a way that doesn't require the user to know what Markdown is.

~3 days, two of them blocked on v0.5 steps 1–3 (settings UI refactor).

---

## v0.5 — UI extensibility + non-technical onboarding (post-v0.4)

The current web layer is 172 lines of FastAPI + 180 lines of Jinja templates + zero JS/CSS files. It does what it does — list updates, edit notes, basic auth. The bottleneck for "easy for non-technical users" is not the templates; it's that **every behaviour that matters lives in `config.yaml` and `.env`**. A SPA pointed at the same backend would expose the same problem.

The decision: **add backend hooks so configuration becomes a UI surface, but stay on server-rendered HTML + HTMX**. No React, no build step, no node_modules in a Python container. Full reasoning in `docs/2026-05-12-ui-and-roadmap-decisions.md`.

Sequencing (each step ships independently — stop after any one and reassess):

1. ✅ **Split `web.py` into a `web/` package.** One routes file per page: `routes_updates.py`, `routes_notes.py`, `routes_settings.py`, `routes_interview.py`. Mechanical refactor, ~1 hour, zero behaviour change. Blocker for everything below.
2. ✅ **`/api/settings/*` read endpoints.** Returns the current YAML-merged config per Pydantic submodel (`/api/settings/llm`, `/api/settings/outputs/notion`, …). Read-only first — no write yet.
3. ✅ **`/api/settings/*` write endpoints + `config.user.yaml` overlay.** Writes a sibling file next to `config.yaml`. Deploy default stays in `config.yaml`; user-edits go to `config.user.yaml`; latter wins on load. Diffable, version-controllable, no DB. Unlocks interview mode UI.
4. ✅ **Schema-driven settings forms.** Render one HTMX form per Pydantic submodel from `model.model_json_schema()`. Adding a new config field becomes "add to the model, no template change" — which is the user's first goal ("add features without touching much code each time"). Includes the model selector + API-key editor.
5. ✅ **Connection-test buttons.** "Test LLM endpoint", "Test Notion DB", "Test Telegram chat id" — each calls the existing client with a tiny no-op. Saves the user a debugging round-trip when they paste a bad key.
6. ✅ **First-run wizard.** New `/setup` page shown when `config.user.yaml` doesn't exist. Three steps: pick LLM (test connection live), enable Docker/HA plugins, optional outputs. End state: writes `config.user.yaml`, drops a marker file, future visits go to `/`.
7. ✅ **Interview mode UI** (from v0.4.2): banner + inline answer form, depends on steps 1–3.

Adjacent improvements (cheap, slot in anywhere after step 1):

- ✅ **Plugin enable/disable toggles.** Plugin and output system is already file-per-thing; surfacing on/off is trivial.
- ✅ **Per-target dry-run preview.** UI button → "preview the note for this container" without writing. Closes the loop on curator honesty in a way the user can see.
- ✅ **"What I see" diagnostics page.** Lists which containers HomelabSage detected, which were skipped (with the matching `skip:` regex), which have `repo:` resolved, which would be curated. Zero engine changes, huge first-run sanity boost.
- ✅ **Settings export / import.** Once the UI writes `config.user.yaml`, expose download + load. Reduces "fear of clicking around" — they can always restore a known-good blob.

### Explicitly NOT in v0.5

- ❌ Rewrite in React/Vue. Current templates render in 180 lines total. A SPA + API split costs ~800 lines of TypeScript before adding any new behaviour, and HomelabSage is not an offline-first / real-time / mobile-app product.
- ❌ User-settings database. Pydantic config + YAML is the right shape (diffable, restorable, version-controllable). A settings table is just worse YAML.
- ❌ Multi-user login UI. HTTP Basic + reverse proxy is sufficient for a self-hosted single-user tool. Revisit when a real second user appears.

---

## Distribution & packaging (post-v0.2.0)

Once the detector layer ships and the project has more than a handful of users, broaden the ways people can install it. Not a priority while the audience is small — packaging churn is wasted effort if the API still shifts.

- 🤔 **Unraid Community Apps template.** Convert the existing `compose.yaml` to an Unraid CA XML plugin so Unraid users can install with one click. Use [`unposer`](https://github.com/unraiders/unposer) — a docker-compose.yml → Unraid CA XML converter — instead of hand-writing the template. Defer until the project is past v0.2.0 (the detector layer changes env vars and mount expectations; converting earlier means redoing the template).

---

## Backlog (uncertain ROI — revisit after user feedback)

- 🤔 **Proactive auditor.** A meta-feature that synthesises every existing detector into a single "homelab health report": stale/abandoned repos (`repo_health`), better-maintained image alternatives (`find_alternatives`), forgotten containers (`orphan_since_days`), port conflicts / new volumes / capability diffs (v0.2.x), curator notes (v0.4), watched repos (v0.3.x). The output is one prioritised list per scan: *"3 services on archived upstreams, 1 better-maintained alternative, 2 orphans, 4 missing notes."* Each recommendation MUST cite the source signal verbatim — no LLM-invented advice. The point isn't to add new detection; it's to make the existing signals legible to a non-technical user who won't read 40 Notion rows. **Suggested placement: v0.6.x**, because the value depends on having most of v0.2/v0.3/v0.4 detectors actually shipping data. Implementing earlier means a thin, mostly-empty report — the dependency is real, not aesthetic. Implementation shape: new output module `outputs/audit.py` that runs after the analyzer, writes a single Markdown to `notes/audit.md` (consumed by the curator on the next pass too — recursive context wins) and exposes `/audit` in the UI. Risk: degenerates into "AI advice without grounding" if any recommendation can fire without a concrete signal. Mitigation: a hard rule that every line of the report links to the row, repo URL or note it came from, plus a CI check on the prompt that rejects responses without citations.
- 🤔 **On-demand URL analysis.** UI input where the user pastes any link and HomelabSage analyses it against the current setup. Four sub-cases, sorted by implementation cost: (a) **GitHub repo URL** — reuses `repo_metadata` + `latest_release` + `repo_health` end-to-end; a thin wrapper over what's already shipped. Cheap. (b) **Docker image URL** — reuses `find_alternatives` plus tag list and (optionally) `docker manifest inspect` for the EXPOSE / VOLUME / cap_add diffs. Medium. (c) **News article / changelog** — fetch + extract main content (`trafilatura` or `readability-lxml`), pipe through the analyzer with the user's notes for context. Medium. (d) **HuggingFace model card** — fetch from HF API, parse the model card (parameter count, quantisation, context length), cross with the system probe (total VRAM/RAM from `system.md` curator, v0.4.1) to flag "this won't load on your hardware" or "this fits with 4 GiB to spare", optionally pull benchmarks from the [Open LLM Leaderboard](https://huggingface.co/spaces/open-llm-leaderboard/open_llm_leaderboard). High — requires the HF model card schema, careful VRAM estimation, and benchmark fetching that may rate-limit. Web search to ground anything beyond the URL itself: prefer SearXNG when the user has it configured (privacy + no API key); fall back to the LLM provider's web tool (Gemini search grounding, Tavily through OpenRouter, etc) when SearXNG isn't available. **Suggested placement: split** — sub-cases (a) and (b) land in **v0.5.x** as a natural extension of the settings UI (one more input mode, same analyzer pipeline). Sub-case (c) joins them once the article extraction library is picked. Sub-case (d) lives in **v0.6.x** alongside the auditor, because the VRAM/benchmark synthesis is its own engineering chunk and overlaps with the auditor's "synthesise concrete signals" pattern. Common scaffold both share: a URL classifier (route by host + path), an HTTP fetch layer with caching, and a small `/analyse` UI surface.
- 🤔 **New-software discovery (cross awesome-selfhosted with user notes).** Right shape, wrong moment. Honest implementation requires either embeddings over `awesome-selfhosted` (new dependency surface) or per-category curated lists (manual maintenance forever). Both are real work and the value depends on the curator having already written good notes per service — which is v0.4. Build v0.4 first, see whether curator-produced notes are rich enough to drive matching, then revisit. If we do it, the safe shape is: weekly crawl of `awesome-selfhosted` → categorised dataset; for each existing note the LLM extracts a `(category, primary_function)` tuple (cached); match function-to-function; only emit a suggestion when the alternative is in the same category, has more recent activity than the user's installed image, AND the LLM can write a one-line reason citing both inputs verbatim. Anything that can't satisfy the citation rule is dropped, not invented. ~1 week minimum. Park until v0.4.x lands.
- 🤔 **PUID/PGID change detector.** Edge case; almost everyone is on LSIO images that handle this cleanly. Worth doing only if a real incident surfaces.
- 🤔 **Rate-limit / polling-cadence diff for HA integrations.** Very narrow audience.
- 🤔 **Hierarchical LLM pipeline (Extractor → Architect → Redactor) for long changelogs.** Add only when the current 32K-context single-pass is empirically insufficient. Don't pre-optimise.
- 🤔 **RAG over PR titles + git diff between versions.** Promising for repos with poor changelogs, but expensive (clone or GitHub API at scale + embedding store). Worth it only if a handful of watched repos consistently produce useless release notes.
- 🤔 **Bloatware detector.** Concept needs sharpening: what counts as "changing the paradigm of a container"? Without a measurable definition this drowns in false positives. Park until someone proposes a concrete signal (e.g. *"new top-level service in the entrypoint"*, *"image size +200%"*).
- 🤔 **Silent CPU fallback from ROCm/CUDA mismatch.** Cannot detect from release notes alone — needs a runtime probe (does the container actually expose the GPU after restart?). Belongs in a separate "post-update health check" feature, not the analyzer.
- 🤔 **Server chronicle (narrative Markdown of major changes).** Beautiful idea, modest operational value. If we ship the self-curating notes (v0.4), the chronicle is one cron job + one prompt away — keep until then.

---

## Rejected (with reasoning, so we don't reopen the discussion)

- ❌ **"Breaking changes you avoided" counter in the UI.** Cannot be measured honestly: we'd be claiming credit for hypothetical incidents. Marketing metric, not a user-useful one. If the user wants a sense of value, the weekly digest already shows the work being done.

---

## How we decide what's next

For each candidate feature, before promoting it from 🤔 to ✅, answer:

1. **Cost vs value** — does this take a day or a week, and how many users does it help?
2. **Reusability** — does it slot into the existing engine/plugin contract, or does it require a new architectural layer?
3. **Honesty** — does it claim to detect something it can actually detect, or is it heuristic dressed as truth?

The roadmap is opinionated on purpose. Issues / PRs proposing items currently parked in 🤔 or ❌ are welcome — but include the answer to those three questions.
