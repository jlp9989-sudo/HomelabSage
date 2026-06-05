"""HomelabSage CLI — Typer app with one module per command.

Façade over the split submodules. Every command registers itself with the
shared `app` instance from `_common` at import time, so listing the
imports here is the registration order.

Public surface (kept stable for `from homelabsage.cli import app`):

    app   → typer.Typer entry-point used by the `homelabsage` console script
"""

from __future__ import annotations

# Order matters: each submodule decorates the shared `app` from `_common`.
# Import them all so the registrations happen at package load.
from . import (  # noqa: F401  (imported for side-effect: command registration)
    analyse,
    audit,
    audit_prune,
    backup,
    check,
    chronicle,
    compose_graph,
    csi,
    curate,
    digest,
    doctor,
    env_diff,
    export,
    health,
    history,
    init,
    interview,
    list_cmd,
    log_anomaly,
    notify,
    notion_archive,
    scan_diff,
    scripts,
    serve,
    snooze,
    stack,
    status,
    tls_check,
    watched,
    watchtower,
)
from ._common import app

__all__ = ["app"]


if __name__ == "__main__":
    app()
