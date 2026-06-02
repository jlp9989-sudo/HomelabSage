"""`homelabsage export` — sanitised dump of containers + recent analyses."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from ..config import load_config
from ..db import Database
from ._common import (
    CONFIG_OPT,
    EXPORT_OUTPUT_OPT,
    EXPORT_REDACT_OPT,
    VERBOSE_OPT,
    app,
    console,
    setup_logging,
)

log = logging.getLogger(__name__)


def _collect_export_payload(cfg) -> dict:
    """Build the unsanitised export payload — kept thin so the redaction layer
    in `homelabsage.redact` is the only thing we have to unit-test for safety.
    """
    from .. import __version__ as version_str

    payload: dict = {
        "homelabsage_version": version_str,
        "containers": [],
        "recent_updates": [],
    }

    if cfg.sources.docker.enabled:
        try:
            import docker as docker_sdk

            client = docker_sdk.DockerClient(
                base_url=f"unix://{cfg.sources.docker.socket.lstrip('/')}"
            )
            for c in client.containers.list(all=True):
                env_list = (c.attrs.get("Config") or {}).get("Env") or []
                env_dict: dict[str, str] = {}
                for line in env_list:
                    if "=" in line:
                        k, v = line.split("=", 1)
                        env_dict[k] = v
                payload["containers"].append({
                    "name": c.name,
                    "image": c.image.tags[0] if c.image.tags else "",
                    "status": (c.attrs.get("State") or {}).get("Status", ""),
                    "ports": list(
                        ((c.attrs.get("NetworkSettings") or {}).get("Ports") or {}).keys()
                    ),
                    "mounts": [
                        {"src": m.get("Source"), "dst": m.get("Destination"), "rw": m.get("RW")}
                        for m in (c.attrs.get("Mounts") or [])
                    ],
                    "env": env_dict,
                    "labels": (c.attrs.get("Config") or {}).get("Labels") or {},
                    "restart_policy": ((c.attrs.get("HostConfig") or {})
                                       .get("RestartPolicy") or {}).get("Name", ""),
                })
        except Exception as e:
            log.warning("Docker inventory failed in export: %s", e)

    db = Database(cfg.storage.database_path)
    for it in db.list(limit=50):
        payload["recent_updates"].append({
            "source": it.update.source,
            "subject": it.update.subject,
            "current_version": it.update.current_version,
            "new_version": it.update.new_version,
            "release_url": it.update.release_url,
            "severity": it.analysis.severity.value if it.analysis else None,
            "summary": it.analysis.summary if it.analysis else None,
            "status": it.status.value,
            "context": it.update.context,
        })
    db.close()

    return payload


@app.command()
def export(
    config: Path = CONFIG_OPT,
    output: Path = EXPORT_OUTPUT_OPT,
    redact: bool = EXPORT_REDACT_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Dump containers + recent analyses as one JSON file, safe to share.

    With `--redact` (default) every value goes through the sanitiser in
    `homelabsage.redact`: IPs become `10.0.0.N`, hostnames `host-N`, and
    keys matching `*_PASSWORD|*_TOKEN|*_KEY|*_SECRET|*_AUTH` or values
    matching common credential shapes (JWT, GitHub PAT, OpenAI sk-…) become
    `<redacted>`. Allowlist preserves loopback IPs and public services like
    `github.com` / `ghcr.io` / `api.openai.com`.
    """
    setup_logging(verbose)
    cfg = load_config(config)
    payload = _collect_export_payload(cfg)
    if redact:
        from ..redact import Sanitiser

        payload = Sanitiser().sanitise(payload)

    text = json.dumps(payload, indent=2, default=str)
    if str(output) == "-":
        sys.stdout.write(text + "\n")
    else:
        output.write_text(text + "\n")
        console.print(f"[green]Wrote[/green] {len(text)} bytes to {output}")
