"""Tests for v0.10.4: I6 (log_anomaly regex tighten) + I7 (compose_lint LSIO pin)."""

from __future__ import annotations

from pathlib import Path

from homelabsage.compose_lint import lint_compose_file
from homelabsage.log_anomaly import count_errors_warns

# ─── I6: regex no longer matches err/crit substrings ──────────────


def test_log_regex_skips_bare_err_in_go_style_lines():
    """Go-style lines like `err = nil` are the high-volume source of
    false positives the old `err` alternative caught. After v0.10.4
    we require the full word `error` so these become silent."""
    lines = [
        "logger: err = nil",                 # bare `err` word → old regex hit
        "main: if err != nil { return err }",
        "trace: err handled cleanly",
    ]
    err, _warn = count_errors_warns(lines)
    # None of these have the full word `error` / `fatal` / etc. The old
    # regex with the `err` alternative matched all three.
    assert err == 0, f"expected 0 (no full-word error), got {err}"


def test_log_regex_skips_bare_crit_word():
    """The `crit` alternative also matched the bare word `crit` — common
    in syslog severity tokens. After tighten we require `critical`."""
    lines = [
        "syslog priority=crit thing",
        "abbrev: crit hit",
    ]
    err, _warn = count_errors_warns(lines)
    assert err == 0, f"expected 0 (no full-word critical), got {err}"


def test_log_regex_still_matches_real_errors():
    """Make sure the tighten didn't kill the actual signal."""
    lines = [
        "ERROR: connection refused",
        "panic: stack overflow",
        "Exception: KeyError 'foo'",
        "FATAL: disk full",
        "Traceback (most recent call last):",
        "CRITICAL: heartbeat missed",
    ]
    err, _warn = count_errors_warns(lines)
    assert err == 6, f"expected all 6 hard-error lines, got {err}"


def test_log_regex_warn_count_unchanged():
    """WARN regex untouched — still catches `warning` and `warn` (which
    is fine on its own; the rate matters not the substring noise)."""
    lines = [
        "WARN: slow query",
        "warning: deprecated API",
        "main: warned the operator",  # `warned` does match \b(warn(ing)?)\b
    ]
    _err, warn = count_errors_warns(lines)
    # The two intentional warns; the third "warned" also matches because
    # \b(warn(ing)?)\b — that's existing behavior we're not changing.
    assert warn >= 2


# ─── I7: LSIO heuristic only fires on actual LSIO images ──────────


_LSIO_BIND_NO_USER = """
services:
  sonarr:
    image: lscr.io/linuxserver/sonarr:latest
    volumes:
      - /mnt/cache/sonarr:/config
      - /mnt/user/Media/TV:/data
"""

_NON_LSIO_POSTGRES = """
services:
  db:
    image: postgres:16
    volumes:
      - /mnt/cache/postgres:/var/lib/postgresql/data
"""

_NON_LSIO_REDIS = """
services:
  cache:
    image: redis:7-alpine
    volumes:
      - /mnt/cache/redis:/data
"""

_LSIO_LEGACY_DOCKERHUB = """
services:
  radarr:
    image: linuxserver/radarr:latest
    volumes:
      - /mnt/cache/radarr:/config
"""

_LSIO_WITH_USER = """
services:
  sonarr:
    image: lscr.io/linuxserver/sonarr:latest
    user: "1000:1000"
    volumes:
      - /mnt/cache/sonarr:/config
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def test_lsio_bind_no_user_fires_on_lscr_io(tmp_path):
    p = _write(tmp_path, "lsio.yml", _LSIO_BIND_NO_USER)
    findings = lint_compose_file(p)
    rules = {f.rule for f in findings}
    assert "bind_no_user" in rules


def test_lsio_bind_no_user_fires_on_legacy_dockerhub(tmp_path):
    p = _write(tmp_path, "lsio-legacy.yml", _LSIO_LEGACY_DOCKERHUB)
    findings = lint_compose_file(p)
    rules = {f.rule for f in findings}
    assert "bind_no_user" in rules


def test_lsio_bind_silent_on_postgres(tmp_path):
    """Postgres binds to /var/lib/postgresql/data — the OLD heuristic
    didn't fire on this anyway (we only matched `/data` and `/config`
    suffixes), so this guards we still don't fire."""
    p = _write(tmp_path, "pg.yml", _NON_LSIO_POSTGRES)
    findings = lint_compose_file(p)
    rules = {f.rule for f in findings}
    assert "bind_no_user" not in rules


def test_lsio_bind_silent_on_redis_with_data_mount(tmp_path):
    """Regression for the real false-positive: redis binds to `/data`
    and has no `user:` — but it's not an LSIO image, so we don't fire.
    Before v0.10.4 this triggered."""
    p = _write(tmp_path, "redis.yml", _NON_LSIO_REDIS)
    findings = lint_compose_file(p)
    rules = {f.rule for f in findings}
    assert "bind_no_user" not in rules, (
        f"redis is not LSIO; should not fire bind_no_user. Got: {rules}"
    )


def test_lsio_bind_silent_when_user_is_set(tmp_path):
    p = _write(tmp_path, "lsio-user.yml", _LSIO_WITH_USER)
    findings = lint_compose_file(p)
    rules = {f.rule for f in findings}
    assert "bind_no_user" not in rules
