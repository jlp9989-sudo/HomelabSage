"""Per-curator-write git auto-commit for `notes_dir`.

The curator writes one Markdown file per service. When `notes_dir` is
itself a git working tree, this module auto-stages + commits the file
after each write. Why this is worth a module:

  - The user can `git log notes/mealie.md` and read the timeline of how
    the curator's understanding of a service evolved.
  - The weekly digest can produce a "what the curator changed this
    week" section by `git diff --since=7d notes/`.
  - Mistakes are recoverable: `git checkout HEAD~1 notes/mealie.md`
    restores the prior body the user trusted.

Everything is best-effort. Missing `git` binary, non-repo `notes_dir`,
or commit failures NEVER raise — the curator's caller already handled
the write; this is bookkeeping.

Operations are split out into pure functions so the engine + curator
can call them without owning subprocess details.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Hard cap — git commit on the curator's hot path. We don't want a
# pathological repo (huge .git, network-mounted) to stall the scan.
_TIMEOUT = 15.0


def is_git_repo(notes_dir: str | Path) -> bool:
    """True iff `notes_dir/.git` exists (or `notes_dir` itself is a worktree).

    `git rev-parse --is-inside-work-tree` is the canonical check but
    we want to short-circuit without spawning a subprocess in the
    common "user hasn't set up git" case.
    """
    if not notes_dir:
        return False
    p = Path(notes_dir)
    return (p / ".git").exists()


def _run_git(
    args: list[str], *, cwd: Path, timeout: float = _TIMEOUT,
) -> tuple[int, str, str] | None:
    """Run a git command. None when git is missing; tuple otherwise."""
    if not shutil.which("git"):
        return None
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, timeout=timeout, check=False,
            capture_output=True, text=True, errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.debug("notes_git: %s failed: %s", args, e)
        return None
    return result.returncode, result.stdout, result.stderr


def auto_commit(
    notes_dir: str | Path,
    *,
    files: list[Path] | None = None,
    message: str,
    author: str = "homelabsage <noreply@homelabsage.local>",
) -> bool:
    """Stage `files` (or all changes if None) and commit with `message`.

    Returns True iff a commit was actually created. False covers:
      - notes_dir isn't a git repo
      - git binary missing
      - nothing to commit (silent — re-runs are idempotent)
      - subprocess failure (logged at DEBUG)

    `author` is fixed-string by default so consecutive curator commits
    in the log show a stable identity. The user can override per-call
    if they want their own name attached.
    """
    p = Path(notes_dir)
    if not is_git_repo(p):
        return False

    if files:
        # Stage only the explicit list. Relative paths so git stores
        # them correctly even when `notes_dir` lives on a different fs.
        rels: list[str] = []
        for f in files:
            try:
                rel = Path(f).resolve().relative_to(p.resolve())
            except ValueError:
                log.debug("notes_git: %s is outside %s, skipping", f, p)
                continue
            rels.append(str(rel))
        if not rels:
            return False
        if _run_git(["add", "--", *rels], cwd=p) is None:
            return False
    else:
        if _run_git(["add", "-A"], cwd=p) is None:
            return False

    # Check there's anything to commit (git returns 1 if nothing staged).
    diff = _run_git(["diff", "--cached", "--quiet"], cwd=p)
    if diff is None:
        return False
    if diff[0] == 0:
        # Nothing staged — happens when the curator wrote the same body
        # twice in a row (idempotent re-curate). Not an error.
        return False

    # Author may be either "Name <email>" or just a free-form string.
    # When the angle-bracket form is missing we fall back to a default
    # placeholder email so `git commit -c user.email=` never raises.
    if "<" in author and ">" in author:
        name = author.split("<", 1)[0].strip() or "homelabsage"
        email = author.split("<", 1)[1].split(">", 1)[0].strip() or \
            "noreply@homelabsage.local"
    else:
        name = author.strip() or "homelabsage"
        email = "noreply@homelabsage.local"
    commit = _run_git(
        ["-c", f"user.name={name}",
         "-c", f"user.email={email}",
         "commit", "-m", message],
        cwd=p,
    )
    if commit is None or commit[0] != 0:
        log.debug("notes_git: commit failed: %s",
                  commit[2] if commit else "no git")
        return False
    return True


def commit_curator_write(
    notes_dir: str | Path,
    *,
    target_kind: str,
    target_name: str,
    update_id: str | None = None,
    paths: list[Path] | None = None,
) -> bool:
    """High-level helper the curator calls after a write.

    The commit message follows a stable format so `git log --oneline`
    is grep-friendly:

        curator: <target_kind>=<target_name> [update=<id>]
    """
    msg_parts = [f"curator: {target_kind}={target_name}"]
    if update_id:
        msg_parts.append(f"update={update_id}")
    return auto_commit(notes_dir, files=paths, message=" ".join(msg_parts))


__all__ = [
    "auto_commit",
    "commit_curator_write",
    "is_git_repo",
]
