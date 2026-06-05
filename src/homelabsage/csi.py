"""CSI mode — post-mortem assistant for a single container.

Use case: "my container is sad, what changed and what does the log say?"

Pipeline:
  1. Look up the most recent stored update for `container_name`.
  2. Pull `docker logs --since=<that update's detected_at>`.
  3. Filter to ERROR / WARN / FATAL lines (with a sane noise floor).
  4. Read any matching notes for the container (same path the analyzer uses).
  5. Build a focused prompt and ask the LLM to diagnose what likely broke
     and what to try next.

The output is a Markdown body printed to stdout. The autonomous variant
(nightly run over every `exited` container) is the same code wrapped in a
scheduler hook — out of scope for this version; CLI first.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ._time import utcnow
from .config import Config, get_active_llm_config
from .db import Database
from .llm import LLMClient
from .models import AnalyzedUpdate
from .notes import NotesProvider

log = logging.getLogger(__name__)


# Liberal regex: matches lines tagged like "ERROR", "[error]", "ERR:",
# "WARNING", "WARN", "FATAL", "panic:", "Traceback", "Exception". Tuned for
# the dialects of common server / language stdlib loggers. Case-insensitive.
_INTERESTING_RE = re.compile(
    r"\b(error|err|warn(ing)?|fatal|panic|traceback|exception|crit(ical)?)\b",
    re.IGNORECASE,
)
# Filter out timestamp-only or pure-numeric lines that happen to contain
# the word "warning" inside a URL or filename (false positives).
_NOISE_LINE_RE = re.compile(r"^[\s\d:.\-T]+$")


@dataclass
class CsiResult:
    """What `gather_evidence` collected — handed straight to the LLM prompt builder."""

    container_name: str
    last_update: AnalyzedUpdate | None
    log_lines: list[str]
    notes_excerpt: str
    log_since: datetime | None


PROMPT_TEMPLATE = """\
You are diagnosing a container post-incident. Be terse and factual.

# Container
{container_name}

# Last detected update for this container
{update_block}

# Log excerpt (filtered to ERROR/WARN/FATAL, last {n_lines} lines)
{log_block}

# User's notes / past decisions about this container
{notes_block}

Output a Markdown report with three sections, each at most 4 bullets:

## What likely broke
Bullets must cite either a specific log line VERBATIM or the version change.
Do not invent causes the evidence does not support.

## What to try first
Concrete, reversible steps. No "consider X" — use imperatives.

## What to investigate next
Open questions or commands that would prove/disprove the leading hypothesis.

Rules:
- If the evidence is too thin (no logs, no update, no notes), say so plainly in
  one sentence and stop. Do NOT fabricate a diagnosis to fill space.
- Never recommend `--force` rebuilds or data deletion unless the log
  explicitly shows data corruption.
"""


def latest_update_for(db: Database, container_name: str) -> AnalyzedUpdate | None:
    """Return the most recently detected stored update whose subject matches.

    `subject` is the docker plugin's per-container value (container name as
    seen by `docker ps`). We don't fuzzy-match — getting the wrong container
    would mislead the LLM in a high-stakes situation.
    """
    items = db.list(source="docker", limit=200)
    for it in items:
        if it.update.subject == container_name:
            return it
    return None


def filter_interesting(lines: Iterable[str], *, tail: int = 80) -> list[str]:
    """Pull out the ERROR/WARN/FATAL-flavoured lines, capped at `tail`.

    We keep the *last* N matches because incident-relevant signal lives at
    the tail of the log — the first crash usually happens after several
    minutes of normal traffic.
    """
    matched: list[str] = []
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip() or _NOISE_LINE_RE.match(line):
            continue
        if _INTERESTING_RE.search(line):
            matched.append(line)
    return matched[-tail:]


def fetch_docker_logs(
    container_name: str,
    *,
    since: datetime | None,
    socket: str = "/var/run/docker.sock",
    max_lines: int = 4000,
    max_line_bytes: int = 4096,
) -> list[str]:
    """Pull recent logs for a container via the docker SDK.

    `since` is passed straight through. The SDK returns bytes; we decode
    lazily and split on newlines so the caller gets a flat list to filter.
    On any failure (container gone, daemon unreachable) we return [] —
    CSI is best-effort and must degrade gracefully.

    I12 (v1.0 punch list): defence in depth against multi-GB log spills.
    - `max_lines` caps the docker `tail=` parameter (default 4000 keeps
      CSI's existing behaviour).
    - `max_line_bytes` truncates individual lines AFTER decode so a single
      30 KB JSON entry doesn't blow our memory either. log_anomaly
      callers can lower `max_lines` further since they only count regex
      hits and don't ship lines to the LLM.
    """
    import docker as docker_sdk

    try:
        client = docker_sdk.DockerClient(base_url=f"unix://{socket.lstrip('/')}")
    except Exception as e:
        log.warning("docker daemon unreachable: %s", e)
        return []
    try:
        c = client.containers.get(container_name)
    except Exception as e:
        log.warning("container %s not found via docker SDK: %s", container_name, e)
        return []
    try:
        # `since` accepts datetime/int/None. `timestamps=True` adds the per-
        # line timestamp prefix which makes the LLM's quotes self-locating.
        # Bound the total bytes to avoid feeding multi-MB logs into the LLM.
        raw = c.logs(
            since=since, timestamps=True, stdout=True, stderr=True,
            tail=max(1, max_lines),
        )
    except Exception as e:
        log.warning("logs() failed for %s: %s", container_name, e)
        return []
    if isinstance(raw, bytes):
        lines = raw.decode("utf-8", errors="replace").splitlines()
    else:
        lines = [chunk.decode("utf-8", errors="replace") for chunk in raw]
    if max_line_bytes > 0:
        lines = [ln[:max_line_bytes] for ln in lines]
    return lines


def build_update_block(it: AnalyzedUpdate | None) -> str:
    """One paragraph + bullets summarising the last detected update."""
    if it is None:
        return "(no update on record for this container)"
    u = it.update
    a = it.analysis
    lines = [
        f"- detected_at: {it.detected_at.isoformat()}",
        f"- version: {u.current_version} → {u.new_version}",
    ]
    if u.release_url:
        lines.append(f"- release_url: {u.release_url}")
    if a:
        lines.append(f"- severity: {a.severity.value}")
        lines.append(f"- summary: {a.summary}")
        if a.breaking_changes:
            lines.append(
                "- breaking_changes: " + "; ".join(a.breaking_changes[:5])
            )
        if a.recommended_action:
            lines.append(f"- recommended_action: {a.recommended_action}")
    return "\n".join(lines)


def gather_evidence(
    cfg: Config,
    container_name: str,
    *,
    fallback_window_hours: int = 48,
) -> CsiResult:
    """Pull every signal CSI needs into a single struct.

    The `since` timestamp is the last detected update's `detected_at` when
    we have one, otherwise `now - fallback_window_hours` so a never-seen
    container still gets a reasonable log slice.
    """
    db = Database(cfg.storage.database_path)
    try:
        last = latest_update_for(db, container_name)
    finally:
        db.close()

    since = (
        last.detected_at if last else utcnow() - timedelta(hours=fallback_window_hours)
    )
    raw_lines = fetch_docker_logs(
        container_name, since=since, socket=cfg.sources.docker.socket
    )
    interesting = filter_interesting(raw_lines)

    notes_provider = NotesProvider(
        notes_dir=cfg.notes.notes_dir or None,
        extra_docs=cfg.notes.extra_docs,
        max_chars=cfg.notes.max_chars,
    )
    notes_excerpt = notes_provider.context_for(container_name, keywords=[container_name])

    return CsiResult(
        container_name=container_name,
        last_update=last,
        log_lines=interesting,
        notes_excerpt=notes_excerpt or "(no notes for this container)",
        log_since=since,
    )


def build_prompt(result: CsiResult) -> str:
    log_block = (
        "\n".join(result.log_lines) if result.log_lines
        else "(no ERROR/WARN/FATAL lines found in the window)"
    )
    return PROMPT_TEMPLATE.format(
        container_name=result.container_name,
        update_block=build_update_block(result.last_update),
        n_lines=len(result.log_lines),
        log_block=log_block,
        notes_block=result.notes_excerpt,
    )


async def run_csi(cfg: Config, container_name: str) -> tuple[str, CsiResult]:
    """End-to-end CSI: gather → prompt → LLM. Returns `(report_markdown, evidence)`.

    Falls back to a plain "insufficient evidence" report when the LLM is
    disabled or unreachable — the caller still gets the gathered context to
    poke at manually.
    """
    evidence = gather_evidence(cfg, container_name)
    llm = LLMClient(get_active_llm_config(cfg))
    if not llm.is_enabled():
        return _evidence_only_report(evidence), evidence
    prompt = build_prompt(evidence)
    raw = await llm.generate_text(prompt, temperature=0.0)
    if not raw:
        return _evidence_only_report(evidence), evidence
    return raw.strip(), evidence


def _evidence_only_report(evidence: CsiResult) -> str:
    """Render a Markdown report from the evidence alone — used when the LLM is
    disabled or fails. Lets `csi` still be useful in air-gapped homelabs."""
    lines = [
        f"# CSI evidence — {evidence.container_name}",
        "",
        "## Last detected update",
        build_update_block(evidence.last_update),
        "",
        "## Log excerpt",
    ]
    if evidence.log_lines:
        lines.append("```")
        lines.extend(evidence.log_lines)
        lines.append("```")
    else:
        lines.append("(no ERROR/WARN/FATAL lines found in the window)")
    lines += [
        "",
        "## Notes excerpt",
        evidence.notes_excerpt,
        "",
        "_LLM analysis unavailable — providing raw evidence only._",
    ]
    return "\n".join(lines)


def run_csi_blocking(cfg: Config, container_name: str) -> tuple[str, CsiResult]:
    return asyncio.run(run_csi(cfg, container_name))
