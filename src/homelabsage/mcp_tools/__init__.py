"""MCP tool implementations + the assembled TOOLS registry.

Split from a single 1,712-line module in v0.13.2 into one file per
domain. Each module owns its `_tool_*` impls AND their JSON-Schema
descriptors (a `TOOLS` fragment), so adding a tool touches exactly one
file. `mcp.py` keeps importing `TOOLS` from here unchanged.
"""

from __future__ import annotations

from .audit_tools import TOOLS as _audit_tools
from .ops import TOOLS as _ops_tools
from .probes import TOOLS as _probe_tools
from .updates import TOOLS as _update_tools

TOOLS: dict[str, dict] = {
    **_probe_tools,
    **_update_tools,
    **_audit_tools,
    **_ops_tools,
}

__all__ = ["TOOLS"]
