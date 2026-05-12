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
- ✅ **Orphan-image detector.** Containers `Status=exited` for >30 days with active updates pending. Surfaces "I forgot this existed and it's full of CVEs" cases.
- ✅ **Parity-aware notification gate.** On Unraid, check `/proc/mdstat` (or `mdcmd status` parsed) before pushing notifications. If a parity check / disk rebuild is running, queue notifications for after. Specific to Unraid users but trivial.
- ⚠️ **Renamed/deprecated env-var detector.** Doable, but requires the LLM to parse the changelog rather than a deterministic diff (env vars aren't declared anywhere machine-readable). Implement as a **prompt rule** in `engine.py` ("if the changelog mentions env-var renames, list them as a `breaking_changes` entry"), not a separate detector. Cheaper, equally effective.
- ⚠️ **Silent DB migration warning.** Same approach: prompt rule, not detector. Add to the LLM system prompt: *"If the release notes mention schema migration, ALTER TABLE, or one-shot data backfills, set `recommended_action` to a step that includes 'do not interrupt the first start after upgrade'."* Zero new code, big behavioural change.

---

## v0.3.x — Cross-cutting features (medium-term)

Bigger changes. Each one is a multi-day chunk.

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

---

## Distribution & packaging (post-v0.2.0)

Once the detector layer ships and the project has more than a handful of users, broaden the ways people can install it. Not a priority while the audience is small — packaging churn is wasted effort if the API still shifts.

- 🤔 **Unraid Community Apps template.** Convert the existing `compose.yaml` to an Unraid CA XML plugin so Unraid users can install with one click. Use [`unposer`](https://github.com/unraiders/unposer) — a docker-compose.yml → Unraid CA XML converter — instead of hand-writing the template. Defer until the project is past v0.2.0 (the detector layer changes env vars and mount expectations; converting earlier means redoing the template).

---

## Backlog (uncertain ROI — revisit after user feedback)

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
