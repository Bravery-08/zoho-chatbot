# backend/zoho_mcp/client.py
"""
Thin async wrapper around an MCP streamable-HTTP session to the Zoho MCP server,
plus helpers to convert MCP tool definitions into Groq/OpenAI function-tool
schemas and to flatten tool results into text.

This is the only place that talks the MCP protocol. Everything downstream
(smoke test, eval harness, and later the agent loop) goes through here.
"""
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from zoho_mcp.config import ZOHO_MCP_URL, build_auth_headers

log = logging.getLogger(__name__)


class ZohoMCPClient:
    """
    Usage:
        async with ZohoMCPClient() as zoho:
            tools = await zoho.list_tools()
            result = await zoho.call_tool("zohobooks__list_invoices", {"status": "unpaid"})
    """

    def __init__(self, url: str | None = None, headers: dict | None = None):
        self.url = url or ZOHO_MCP_URL
        self.headers = headers if headers is not None else build_auth_headers()
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "ZohoMCPClient":
        if not self.url:
            raise RuntimeError("ZOHO_MCP_URL is not set — add it to your .env.")
        # streamablehttp_client yields (read_stream, write_stream, get_session_id)
        read, write, _ = await self._stack.enter_async_context(
            streamablehttp_client(self.url, headers=self.headers)
        )
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        log.info("Connected to Zoho MCP server at %s", self.url)
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()
        self.session = None

    async def list_tools(self) -> list:
        assert self.session is not None, "Not connected — use 'async with ZohoMCPClient()'."
        resp = await self.session.list_tools()
        return list(resp.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
        assert self.session is not None, "Not connected — use 'async with ZohoMCPClient()'."
        return await self.session.call_tool(name, arguments=arguments or {})


# ── Schema / result helpers ───────────────────────────────────────────────────

# Types whose strict validation Groq rejects when the model generates strings.
# We retype these as plain "string" so Groq accepts whatever the model emits;
# Zoho's REST API coerces query-string values server-side regardless of type.
_PRIMITIVE_TYPES_TO_COERCE = {"integer", "number", "boolean"}

# Constraints that can trigger independent Groq validation failures even when
# the type is correct (e.g. minimum:1 fails when model passes 0 as a string).
_CONSTRAINTS_TO_STRIP = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "minItems", "maxItems",
    "format", "default",
}


def _sanitize_schema(schema: dict) -> dict:
    """
    Recursively make Zoho MCP tool schemas safe for Groq's strict validator.

    Problems in Zoho's raw schemas
    ───────────────────────────────
    1. ``integer`` / ``number`` / ``boolean`` fields — the model generates
       string representations ("1", "true").  Groq rejects these with 400.
       ``anyOf: [integer, string]`` does NOT help: Groq's validator tries each
       branch in order, still rejects on the integer branch, and stops.
       Fix: retype these fields as plain ``"string"``.

    2. ``minimum`` / ``maximum`` / ``format`` / ``default`` etc. — independent
       constraints that trigger 400s even when the type would be fine.
       Fix: strip them from every schema node.

    3. ``array``-typed fields (criteria, columns, value[], …) — the model
       may generate a JSON-encoded string instead of an array literal.
       Fix: retype ``array`` as ``"string"`` too; Zoho's API accepts both.

    Why this is safe
    ────────────────
    Zoho's REST layer coerces all query-string and JSON body values.
    ``page="1"`` and ``page=1`` produce identical results.  The semantic
    constraint lives in Zoho, not in the schema we pass to Groq.
    Enum constraints on string fields are preserved so the model still
    receives the allowed-value lists.
    """
    if not isinstance(schema, dict):
        return schema

    schema_type = schema.get("type")

    # ── Leaf types: retype as string, strip numeric / format constraints ──────
    if schema_type in _PRIMITIVE_TYPES_TO_COERCE:
        return {k: v for k, v in schema.items()
                if k not in {"type", *_CONSTRAINTS_TO_STRIP}} | {"type": "string"}

    # ── Array types: also retype as string (model generates JSON strings) ─────
    if schema_type == "array":
        return {k: v for k, v in schema.items()
                if k not in {"type", "items", *_CONSTRAINTS_TO_STRIP}} | {"type": "string"}

    # ── Object / untyped: strip constraints and recurse into children ─────────
    result = {}
    for k, v in schema.items():
        if k in _CONSTRAINTS_TO_STRIP:
            continue
        if k == "properties" and isinstance(v, dict):
            result[k] = {pk: _sanitize_schema(pv) for pk, pv in v.items()}
        elif k in ("items", "additionalProperties") and isinstance(v, dict):
            result[k] = _sanitize_schema(v)
        elif k in ("allOf", "anyOf", "oneOf") and isinstance(v, list):
            result[k] = [_sanitize_schema(s) if isinstance(s, dict) else s
                         for s in v]
        else:
            result[k] = v
    return result


def tool_to_groq_schema(tool) -> dict:
    """Convert one MCP tool definition into a Groq/OpenAI function-tool schema."""
    raw = tool.inputSchema or {"type": "object", "properties": {}}
    parameters = _sanitize_schema(raw)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "").strip(),
            "parameters": parameters,
        },
    }


def tools_to_groq_schema(tools: list) -> list[dict]:
    return [tool_to_groq_schema(t) for t in tools]


def tool_schema_to_dict(tool) -> dict:
    """A JSON-serialisable view of a tool, for dumping schemas to disk."""
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.inputSchema,
    }


def result_to_text(result) -> str:
    """Flatten an MCP call_tool result's content blocks into plain text."""
    if result is None:
        return ""
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    return "\n".join(parts)