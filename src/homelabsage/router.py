"""Agent routing layer — turn a natural-language request into tool calls.

This is the brain of the conversational interface: given a user message
and the live tool catalog (`mcp_tools.TOOLS`), it asks an LLM — under
guided decoding — which tool(s) to invoke, in what order, with what
arguments, and whether the action needs human confirmation before it runs.

Design choices that make this robust across the flaky-at-native-tool-calls
local models we run (Qwen3.6-35B-A3B et al.):

  - **JSON-action pattern, not native `tools`.** We reuse the same
    `response_format: json_schema` guided-decoding path the analyzer uses
    (`LLMClient.complete_structured`). A3B-class models are unreliable at
    OpenAI-style `tool_calls` but solid at emitting a constrained JSON
    object.
  - **`arguments_json` as a string, not a nested object.** Per-tool arg
    schemas are dynamic; a single grammar can't enforce all of them and
    strict-mode forbids open objects. So the model emits arguments as a
    JSON-encoded string (exactly what OpenAI function-calling does) and we
    parse it ourselves. The top-level action object stays flat and
    strict-compatible.
  - **No-hallucination guard.** The parser flags any tool name not in the
    live catalog. The eval harness scores this; the live chat refuses it.
  - **Confirmation gate.** Tools that mutate state are listed in
    `DESTRUCTIVE_TOOLS`; the model is told to set `needs_confirmation=true`
    when it picks one, and the caller must propose → confirm → execute.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .llm import LLMClient

log = logging.getLogger(__name__)


# Tools that change state. The MCP catalog carries no destructive marker
# (every entry is description/params_schema/impl), so we curate the set
# here. Keep it in sync with mcp_tools when write-tools are added; the
# `test_router` suite asserts every name here exists in the live catalog so
# a rename can't silently make a destructive tool look read-only.
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset({
    "set_update_status",
    "set_user_note",
    "set_star",
    "snooze_update",
    "clear_all_snoozes",
    "purge_old_updates",
    "audit_mute_add",
    "audit_mute_remove",
    "audit_mute_purge_expired",
    "audit_prune",
    "clear_pending_dispatches",
})


# Flat, strict-mode-compatible action schema. `arguments_json` is a STRING
# (JSON-encoded) so the grammar stays closed while per-tool args remain
# free-form — see module docstring.
ROUTING_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reasoning": {"type": "string"},
        "needs_confirmation": {"type": "boolean"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tool": {"type": "string"},
                    "arguments_json": {"type": "string"},
                },
                "required": ["tool", "arguments_json"],
            },
        },
        "direct_answer": {"type": ["string", "null"]},
    },
    "required": [
        "reasoning",
        "needs_confirmation",
        "tool_calls",
        "direct_answer",
    ],
}


@dataclass
class ToolCall:
    """One requested tool invocation, after parsing `arguments_json`."""

    tool: str
    arguments: dict
    arguments_raw: str
    arguments_ok: bool       # did arguments_json parse into an object?
    known: bool              # is `tool` in the live catalog?


@dataclass
class RouteDecision:
    """Parsed router output. `valid_json` is the top-level success flag:
    the model returned JSON matching the action shape. Even when False the
    other fields hold best-effort values so the scorer/UI can degrade
    gracefully."""

    reasoning: str
    needs_confirmation: bool
    tool_calls: list[ToolCall]
    direct_answer: str | None
    valid_json: bool
    raw: str = field(repr=False, default="")

    @property
    def hallucinated_tools(self) -> list[str]:
        """Requested tool names that don't exist in the catalog."""
        return [c.tool for c in self.tool_calls if not c.known]

    @property
    def tool_names(self) -> list[str]:
        return [c.tool for c in self.tool_calls]


def _compact_schema(params_schema: dict) -> str:
    """One-line summary of a tool's parameters for the prompt.

    Renders `name(type)[*]` per property (`*` = required) instead of dumping
    the full JSON Schema, keeping the catalog compact enough that all ~50
    tools fit comfortably in context.
    """
    props = (params_schema or {}).get("properties") or {}
    required = set((params_schema or {}).get("required") or [])
    if not props:
        return "(no args)"
    parts = []
    for name, spec in props.items():
        t = spec.get("type", "any")
        if isinstance(t, list):
            t = "|".join(str(x) for x in t)
        enum = spec.get("enum")
        rendered = f"{name}:{t}"
        if enum:
            rendered += "{" + ",".join(str(e) for e in enum) + "}"
        if name in required:
            rendered += "*"
        parts.append(rendered)
    return ", ".join(parts)


def build_catalog_block(
    catalog: dict[str, dict], *, destructive: frozenset[str] = DESTRUCTIVE_TOOLS
) -> str:
    """Render the tool catalog as a stable, deterministic text block.

    Sorted by tool name so the prompt (and therefore any prompt-cache key
    and any recorded golden fixture) is reproducible across runs.
    """
    lines = []
    for name in sorted(catalog):
        spec = catalog[name]
        desc = " ".join((spec.get("description") or "").split())
        flag = " [WRITE — needs confirmation]" if name in destructive else ""
        args = _compact_schema(spec.get("params_schema") or {})
        lines.append(f"- {name}({args}){flag}: {desc}")
    return "\n".join(lines)


_SYSTEM_INSTRUCTIONS = """\
You are the routing brain of HomelabSage, a homelab update advisor. Decide \
how to fulfil the user's request using ONLY the tools listed below.

Rules:
1. Pick the minimal set of tools that answers the request. For a multi-step \
request, list the tool_calls in the exact order they must run.
2. `arguments_json` MUST be a JSON object encoded as a string, with keys \
matching that tool's parameters. Use "{}" when a tool takes no arguments.
3. NEVER invent a tool. If no listed tool fits the request, return an empty \
tool_calls array and explain in `direct_answer` what you can't do. Do not \
guess a plausible-sounding tool name.
4. Set `needs_confirmation` to true if ANY chosen tool is marked \
[WRITE — needs confirmation]; otherwise false. Write tools change state and \
must be confirmed by the human before running.
5. `direct_answer` is for when you answer without tools (or explain a \
refusal); set it to null when you are calling tools.
6. `reasoning` is one short sentence on why you chose those tools.

Return ONLY the JSON action object."""


def build_routing_prompt(
    catalog: dict[str, dict],
    user_message: str,
    *,
    destructive: frozenset[str] = DESTRUCTIVE_TOOLS,
) -> str:
    """Assemble the full routing prompt (instructions + catalog + request)."""
    return (
        f"{_SYSTEM_INSTRUCTIONS}\n\n"
        f"## Available tools\n{build_catalog_block(catalog, destructive=destructive)}\n\n"
        f"## User request\n{user_message.strip()}\n"
    )


def _extract_json_object(raw: str) -> str:
    """Pull the first balanced JSON object out of noisy model text.

    Mirrors `llm._parse_analysis`'s tolerance: strips ```json fences, then
    falls back to first-`{` / last-`}` slicing.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip("\n")
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()
    if not text.startswith("{"):
        i = text.find("{")
        j = text.rfind("}")
        if i >= 0 and j > i:
            text = text[i : j + 1]
    return text


def parse_route(raw: str, *, known_tools: set[str]) -> RouteDecision:
    """Parse raw model text into a `RouteDecision`.

    Tolerant by design — a model that wraps the object in prose or fences
    still parses. A hard JSON failure yields `valid_json=False` with empty
    tool_calls so the caller treats it as "no actionable routing".
    """
    text = _extract_json_object(raw)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("router: non-JSON output: %s", e)
        return RouteDecision(
            reasoning="", needs_confirmation=False, tool_calls=[],
            direct_answer=None, valid_json=False, raw=raw,
        )
    if not isinstance(data, dict):
        return RouteDecision(
            reasoning="", needs_confirmation=False, tool_calls=[],
            direct_answer=None, valid_json=False, raw=raw,
        )

    calls: list[ToolCall] = []
    raw_calls = data.get("tool_calls")
    if isinstance(raw_calls, list):
        for item in raw_calls:
            if not isinstance(item, dict):
                continue
            tool = item.get("tool")
            if not isinstance(tool, str) or not tool:
                continue
            args_raw = item.get("arguments_json", "")
            # Models occasionally emit a nested object instead of a string
            # despite the schema. Accept both.
            args: dict = {}
            args_ok = False
            if isinstance(args_raw, dict):
                args, args_ok, args_raw = args_raw, True, json.dumps(args_raw)
            elif isinstance(args_raw, str):
                s = args_raw.strip()
                if s == "":
                    args, args_ok = {}, True
                else:
                    try:
                        parsed = json.loads(s)
                        if isinstance(parsed, dict):
                            args, args_ok = parsed, True
                    except (json.JSONDecodeError, ValueError):
                        args_ok = False
            calls.append(ToolCall(
                tool=tool, arguments=args, arguments_raw=str(args_raw),
                arguments_ok=args_ok, known=tool in known_tools,
            ))

    direct = data.get("direct_answer")
    if direct is not None and not isinstance(direct, str):
        direct = str(direct)
    return RouteDecision(
        reasoning=str(data.get("reasoning") or ""),
        needs_confirmation=bool(data.get("needs_confirmation", False)),
        tool_calls=calls,
        direct_answer=direct,
        valid_json=True,
        raw=raw,
    )


class Router:
    """Stateless wrapper: message → `RouteDecision` via an `LLMClient`.

    Holds the catalog + destructive set so the web/chat layer and the eval
    harness share one routing implementation. `route` raises on transport
    failure (so the caller can mark a model unreachable); a parse miss is a
    valid `RouteDecision(valid_json=False)`, not an exception.
    """

    def __init__(
        self,
        llm: LLMClient,
        catalog: dict[str, dict],
        *,
        destructive: frozenset[str] = DESTRUCTIVE_TOOLS,
    ):
        self.llm = llm
        self.catalog = catalog
        self.destructive = destructive
        self._known = set(catalog)

    async def route(self, user_message: str) -> RouteDecision:
        prompt = build_routing_prompt(
            self.catalog, user_message, destructive=self.destructive,
        )
        res = await self.llm.complete_structured(
            prompt, schema=ROUTING_SCHEMA, schema_name="route",
        )
        decision = parse_route(res.text, known_tools=self._known)
        return decision
