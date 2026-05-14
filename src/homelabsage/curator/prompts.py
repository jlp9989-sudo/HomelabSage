"""Prompt templates and placeholder utilities for the curator.

Keep this module text-only. No I/O, no class state, no imports from sibling
modules of the curator package — they are free to import from here.
"""

from __future__ import annotations

PROMPT_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "container_name",
        "image",
        "repo",
        "current_version",
        "ports",
        "mounts",
        "env_vars",
        "labels",
        "release_notes",
        "style_examples",
        "user_purpose",
        "readme_excerpt",
        "docker_hub_description",
        "recent_logs",
        "user_context",
    }
)


DEFAULT_PROMPT_TEMPLATE = """\
You are documenting a service running in the user's homelab. Your job is to write a short Markdown note that captures the important facts about this container so a future maintainer (or another tool) can read it in under a minute.

Rules for the note:

1. Open with ONE sentence stating the PURPOSE of this service for the user (the "why" it exists), not what its software does in general.
2. Then add 2 to 5 short bullet points. Each bullet should be a fact a future maintainer would actually need:
   - Version pins, versionlocks, or specific versions known to be broken
   - Critical environment variables, mount paths, or ports
   - Dependencies on other services in the homelab
   - Known traps, workarounds, or quirks
3. Do not restate facts that `docker inspect` would already show (image name, full port list, full env list). Only mention them if they carry a non-obvious meaning.
4. You may group bullets under `## Section Name` headers if it helps reading. Headers are optional.
5. Keep the total note under 30 lines.
6. Output ONLY the Markdown body. No code fences, no preamble, no closing remarks.
7. If you do not have enough information to write the PURPOSE sentence, write exactly this single line and stop: `(no purpose stated yet — fill in)`. Do not invent reasons. EXCEPTION: if the `# User-provided purpose` block below is non-empty, USE IT VERBATIM as the PURPOSE sentence (Rule 1) — Rule 7 does not apply, do NOT emit the fallback line.
8. NEVER invent facts. Do not assign meaning to container name suffixes, image tag variants, env var values, or settings unless the inputs above explicitly state that meaning. If the inputs do not support a bullet, omit the bullet entirely — fewer bullets are better than speculation. In particular:
   - Do not guess what a name suffix like `-pnp`, `-lts`, `-edge`, etc. means.
   - Do not speculate about whether a setting is "overridden", "unused", "ignored", or "deprecated" without evidence in the inputs.
   - Do not invent network behavior, security posture, or integration details that are not in the inputs.
9. NEVER quote specific version numbers, release dates, PR numbers, issue numbers, or commit hashes unless they appear verbatim in the inputs above (in the `# Container facts` block as `current version`, or inside the `# Recent upstream releases` block). Do not extrapolate "the next version", "the latest release", or "released on <date>" — if the input says current version is 2.19.5 and the recent releases block is empty, the only honest statement is "running 2.19.5; no upstream releases visible". Never compute a "+1 minor" or guess a future tag.

# Container facts
- name: {container_name}
- image: {image}
- repo: {repo}
- current version: {current_version}
- ports (published): {ports}
- mounts: {mounts}
- environment variables (secrets redacted): {env_vars}
- labels of interest: {labels}

# Recent upstream releases
{release_notes}

# Upstream README excerpt (authoritative description of the software)
{readme_excerpt}

# Docker Hub description (fallback when no README is available)
{docker_hub_description}

# Recent container logs (live signal — last lines stdout+stderr)
{recent_logs}

# Existing user notes/memory mentioning this container (HIGH-VALUE — your user wrote these about THEIR setup)
{user_context}

# User-provided purpose (authoritative — overrides Rule 7 fallback)
{user_purpose}

# Style examples from the user (study the tone and structure, do not copy the content)
{style_examples}
"""


# Used after a Rule 7 fallback to generate a one-sentence guess that prefills
# the interview answer textarea. Deliberately narrower than the main prompt:
# we are NOT trying to write the user's note here, just to give them a head
# start they can confirm or edit. The "Likely" / "Appears to be" framing
# tells the LLM (and the reader) this is speculation, not asserted fact.
SUGGESTION_PROMPT_TEMPLATE = """\
You are helping a homelab user document a service. Write a SINGLE sentence prefilling their answer to "what is this for in your homelab?". Begin with a hedge word: "Likely", "Appears to be", "Probably". Output the sentence and nothing else — no preamble, no bullets, no markdown, no quotes.

PRIORITISE the user's own notes (`# Existing user notes/memory` block below) over the upstream README — the user knows their setup, the README only describes the software. If the user's notes describe how THEY use it, prefer that wording.

If you genuinely cannot guess from any of the inputs, output exactly: `(no guess)`

# Container facts
- name: {container_name}
- image: {image}
- repo: {repo}
- current version: {current_version}
- ports (published): {ports}
- mounts: {mounts}
- environment variables (secrets redacted): {env_vars}
- labels of interest: {labels}

# Existing user notes/memory mentioning this container (HIGH-VALUE — prefer this over the README)
{user_context}

# Upstream README excerpt
{readme_excerpt}

# Docker Hub description
{docker_hub_description}

# Recent container logs
{recent_logs}
"""


class SafePromptDict(dict):
    """`str.format_map` helper that leaves unknown `{placeholders}` untouched.

    Lets custom prompt templates ignore placeholders they don't care about
    without raising KeyError.
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
