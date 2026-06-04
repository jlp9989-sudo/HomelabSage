"""Container restart-policy auditor.

A container without a restart policy disappears on `docker restart`,
host reboot, or daemon-crash — and the user usually only discovers
it weeks later when something has been silently missing. Docker's
default is `"no"` (i.e. don't restart), so the absence of an
explicit policy is the failure mode.

We read `HostConfig.RestartPolicy.Name`:
  - `""` (truly unset) or `"no"` → flag as `medium`
  - `"on-failure"` with `MaximumRetryCount` between 1-5 → `info`
    (intentional bounded retry, surfaces only when paired with
    `policy_audit_strict=true`)
  - `"always"` / `"unless-stopped"` → no flag

Like the rest of the detectors here, this is pure data on
`container.attrs`. The plugin attaches the result; the analyzer +
auditor surface it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RestartPolicyFinding:
    """Per-container restart-policy verdict."""

    policy_name: str
    max_retry_count: int
    severity: str   # info | medium

    def to_context(self) -> dict:
        return {
            "policy_name": self.policy_name,
            "max_retry_count": self.max_retry_count,
            "severity": self.severity,
        }


def evaluate(
    host_config: dict,
    *,
    strict: bool = False,
) -> RestartPolicyFinding | None:
    """Return a finding when the policy is suspect.

    `strict` flips bounded `on-failure` policies into `info`-level
    flags; default off because those are intentional and the user
    set them deliberately.
    """
    rp = host_config.get("RestartPolicy") if isinstance(host_config, dict) else None
    if not isinstance(rp, dict):
        return None
    name = (rp.get("Name") or "").strip().lower()
    max_retry = int(rp.get("MaximumRetryCount") or 0)
    if name in ("", "no"):
        return RestartPolicyFinding(
            policy_name=name or "(unset)",
            max_retry_count=max_retry,
            severity="medium",
        )
    if name == "on-failure" and strict and 1 <= max_retry <= 5:
        return RestartPolicyFinding(
            policy_name=name,
            max_retry_count=max_retry,
            severity="info",
        )
    # "always", "unless-stopped", or unbounded on-failure → no flag
    return None


__all__ = ["RestartPolicyFinding", "evaluate"]
