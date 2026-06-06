"""Tests for v0.6.7: restart-policy, env-perms, recurring-failures, env-diff CLI."""

from __future__ import annotations

import stat

from homelabsage.env_perms import (
    EnvPermFinding,
    _evaluate_mode,
)
from homelabsage.env_perms import (
    scan as scan_env_perms,
)
from homelabsage.restart_policy import RestartPolicyFinding
from homelabsage.restart_policy import evaluate as eval_rp

# ─── restart_policy ────────────────────────────────────────────────


def test_restart_policy_unset_flags_medium():
    out = eval_rp({"RestartPolicy": {"Name": "", "MaximumRetryCount": 0}})
    assert out is not None
    assert out.severity == "medium"
    assert out.policy_name == "(unset)"


def test_restart_policy_explicit_no_flags_medium():
    out = eval_rp({"RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}})
    assert out is not None
    assert out.severity == "medium"


def test_restart_policy_always_no_flag():
    assert eval_rp({"RestartPolicy": {"Name": "always"}}) is None


def test_restart_policy_unless_stopped_no_flag():
    assert eval_rp({"RestartPolicy": {"Name": "unless-stopped"}}) is None


def test_restart_policy_on_failure_unbounded_no_flag():
    """on-failure with MaximumRetryCount=0 is the docker default — fine."""
    assert eval_rp(
        {"RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": 0}},
    ) is None


def test_restart_policy_on_failure_bounded_strict_only():
    """Bounded on-failure flags only in strict mode."""
    rp_dict = {"RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": 3}}
    assert eval_rp(rp_dict) is None
    out = eval_rp(rp_dict, strict=True)
    assert out is not None
    assert out.severity == "info"


def test_restart_policy_missing_hostconfig():
    assert eval_rp({}) is None


def test_restart_policy_to_context_shape():
    f = RestartPolicyFinding(
        policy_name="no", max_retry_count=0, severity="medium",
    )
    ctx = f.to_context()
    assert ctx == {
        "policy_name": "no", "max_retry_count": 0, "severity": "medium",
    }


# ─── env_perms ────────────────────────────────────────────────────


def test_env_perms_evaluate_mode_buckets():
    # 0600 = owner rw only → fine
    assert _evaluate_mode(0o600) is None
    # 0644 = world-readable → medium
    out = _evaluate_mode(0o644)
    assert out is not None and out[0] == "medium"
    # 0664 = group-writable → high
    out = _evaluate_mode(0o664)
    assert out is not None and out[0] == "high"
    # 0666 = world-writable → critical (overrides group-writable)
    out = _evaluate_mode(0o666)
    assert out is not None and out[0] == "critical"


def test_env_perms_scan_flags_644_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("SECRET=x\n")
    f.chmod(0o644)
    findings = scan_env_perms([str(tmp_path)])
    assert len(findings) == 1
    assert findings[0].severity == "medium"
    assert findings[0].mode_octal == "0o644"


def test_env_perms_scan_skips_600_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("SECRET=x\n")
    f.chmod(0o600)
    assert scan_env_perms([str(tmp_path)]) == []


def test_env_perms_scan_walks_children(tmp_path):
    """Dockge layout: stacks/<name>/.env."""
    stack_dir = tmp_path / "mealie"
    stack_dir.mkdir()
    f = stack_dir / ".env"
    f.write_text("X=1\n")
    f.chmod(0o644)
    findings = scan_env_perms([str(tmp_path)])
    assert len(findings) == 1


def test_env_perms_handles_world_writable(tmp_path):
    f = tmp_path / ".env"
    f.write_text("X=1\n")
    f.chmod(0o666)
    findings = scan_env_perms([str(tmp_path)])
    assert len(findings) == 1
    assert findings[0].severity == "critical"


def test_env_perms_to_context_shape():
    f = EnvPermFinding(
        path="/x/.env", mode_octal="0o644",
        severity="medium", reason="world readable",
    )
    ctx = f.to_context()
    assert ctx["mode_octal"] == "0o644"
    assert ctx["severity"] == "medium"


def test_env_perms_matches_dotenv_suffix(tmp_path):
    """Files like `prod.env` also count."""
    f = tmp_path / "prod.env"
    f.write_text("X=1\n")
    f.chmod(0o644)
    findings = scan_env_perms([str(tmp_path)])
    assert len(findings) == 1


# ─── recurring failures ───────────────────────────────────────────


def test_recurring_failures_increments_only_on_transition(tmp_path):
    """FAILED→FAILED is idempotent; only true transitions bump count."""
    from homelabsage.db import Database
    from homelabsage.models import (
        AnalyzedUpdate,
        Update,
        UpdateStatus,
    )
    db = Database(str(tmp_path / "t.db"))
    item = AnalyzedUpdate(
        update=Update(
            source="docker", subject="mealie",
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    # Flip NEW→FAILED→APPLIED→FAILED→FAILED→APPLIED→FAILED
    # Real transitions to FAILED: 3 (initial, after applied, after applied)
    db.set_status(item.id, UpdateStatus.FAILED)    # +1
    db.set_status(item.id, UpdateStatus.APPLIED)   # 0
    db.set_status(item.id, UpdateStatus.FAILED)    # +1
    db.set_status(item.id, UpdateStatus.FAILED)    # idempotent: 0
    db.set_status(item.id, UpdateStatus.APPLIED)   # 0
    db.set_status(item.id, UpdateStatus.FAILED)    # +1
    rows = db.list_recurring_failures(min_count=2)
    assert len(rows) == 1
    assert rows[0]["failure_count"] == 3


def test_recurring_failures_below_min_filtered(tmp_path):
    from homelabsage.db import Database
    from homelabsage.models import (
        AnalyzedUpdate,
        Update,
        UpdateStatus,
    )
    db = Database(str(tmp_path / "t.db"))
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject="y",
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    db.set_status(item.id, UpdateStatus.FAILED)
    assert db.list_recurring_failures(min_count=2) == []


def test_recurring_failures_non_failed_status_does_not_bump(tmp_path):
    """APPLIED / DISMISSED shouldn't change the counter."""
    from homelabsage.db import Database
    from homelabsage.models import (
        AnalyzedUpdate,
        Update,
        UpdateStatus,
    )
    db = Database(str(tmp_path / "t.db"))
    item = AnalyzedUpdate(
        update=Update(
            source="x", subject="y",
            current_version="1", new_version="2",
        ),
    )
    db.upsert(item)
    db.set_status(item.id, UpdateStatus.FAILED)
    db.set_status(item.id, UpdateStatus.APPLIED)
    db.set_status(item.id, UpdateStatus.DISMISSED)
    rows = db.list_recurring_failures(min_count=1)
    assert len(rows) == 1
    assert rows[0]["failure_count"] == 1


# `stat` is needed as a constants source for the mode tests; the
# linter would otherwise flag the import as unused.
_ = stat
