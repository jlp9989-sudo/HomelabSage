# Example notes file

Drop this file (or any other `.md` / `.txt`) into your `notes_dir` and HomelabSage will inject the relevant section into the LLM prompt for each update it analyzes. Matching is by header + body keyword overlap with the update's `subject` (typically a container or repo name).

**Style rules** — what makes a note actually useful to the analyzer:

1. **Lead with the *why*.** "Pinned at 8.x because RAGFlow needs the legacy auth mechanism" beats "Running 8.x". The model can read the changelog; it can't infer your constraints.
2. **Don't restate what `docker inspect` already shows.** Image name, ports, basic mounts — skip them.
3. **Name the upstream issue / PR if you have one.** It anchors the constraint to something the model can reason about.
4. **One service per `##` header.** Headers count 3× in the matcher; keep the service name visible there.

Delete this file once you've created your own. The matcher is happy with one big `notes.md` or many small per-service files — both work.

---

## Elasticsearch

Pinned at 8.x for as long as RAGFlow stays on the old auth mechanism (upstream issue [infiniflow/ragflow#3812](https://github.com/infiniflow/ragflow/issues/3812)). Do **not** suggest 9.x bumps — they will break vector search until RAGFlow ships a compatible client. Re-evaluate when that issue closes.

## Mealie

Postgres 16 backend, not SQLite — the auto-migrate path was pulled in 2.0 and any "this release migrates from SQLite" note is harmless for us. We *do* care about the OpenAI-prompts feature (we override them in Spanish via `MEALIE_OPENAI_CUSTOM_PROMPT`) — flag any release that touches that env var.

## llama-server (llama.cpp)

Built locally from kyuz0's vulkan-radv toolbox for gfx1151. We pin to the exact `bXXXX` build number that matches the toolbox release; do not recommend "upgrade to latest" without checking the toolbox first. `--reasoning off` is required at server start — config-file `reasoning-budget=0` is silently ignored over the API.

## Home Assistant Core

Major bumps occasionally raise the minimum Python version, which cascades into HACS plugins. Always check the release notes for "minimum Python" and warn if it changed — half our HACS integrations lag by 2-3 weeks behind a Python bump.
