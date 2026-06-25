# backend/zoho_mcp/agent.py
"""
Phase 1 Zoho agent loop.

Flow for each user message
──────────────────────────
1. Fetch sanitised tool schemas from the MCP server (cached for TOOL_CACHE_TTL s).
2. LLM picks the best tool and generates arguments.
3. Inject the real ZOHO_ORG_ID wherever the model wrote a placeholder.
4. Call the Zoho MCP tool — get live data.
5. Grounding check — if Zoho returned an API error, return None → caller escalates.
6. Synthesize a natural-language answer from the raw Zoho JSON.

Returns
───────
str   — synthesized answer to send to the user.
None  — tool errored or no tool matched; caller should route to escalate.
        Empty-but-valid results (e.g. no unpaid invoices) are NOT None —
        the synthesizer says "you have no unpaid invoices."
"""
import json
import logging
import time
from typing import Optional

from groq import Groq

from zoho_mcp.client import ZohoMCPClient, result_to_text, tools_to_groq_schema
from zoho_mcp.config import GROQ_API_KEY, AGENT_MODEL, ZOHO_ORG_ID, TOOL_CACHE_TTL

log = logging.getLogger(__name__)

# ── Tool-selection routing rules (mirrors Phase 0 eval prompt) ───────────────
_ROUTING_PROMPT = """
You are an operations agent for a B2B merchant-export company.
You have access ONLY to the Zoho business data tools provided — no search
engines, calculators, or any other tools. Never call a tool not in the list.

WHEN TO CALL A TOOL: only to look up live Zoho data (contacts, invoices,
orders, stock, shipments, balances). Do NOT call any tool for general
knowledge, "how to" questions, weather, exchange rates, or calculations.

ROUTING RULES — apply when two tools could both fit:

1. CRM LOOKUPS: use ZohoCRM_searchRecords for any search by name, company,
   location, stage, or attribute. Use ZohoCRM_getRecord only when you have
   an explicit numeric record ID in the message.

2. PURCHASE ORDERS: always ZohoInventory_list_purchase_orders.
   Invoices are outbound customer bills; POs are inbound supplier documents.

3. SALES ORDERS — by context:
   • Specific SO number (SO-XXXXX format) → ZohoInventory_get_sales_order.
   • Listing or filtering multiple SOs   → ZohoBooks_list_sales_orders.

4. BALANCES vs INVOICES:
   • Total owed, overall receivables, customer balance →
     ZohoBooks_get_customer_balances_report.
   • Individual invoice, unpaid/overdue invoices → ZohoBooks_list_invoices.

5. SHIPMENTS vs SALES ORDERS: use ZohoInventory_get_shipment_order when the
   question mentions shipment, shipping, or dispatch.

6. AGGREGATIONS: do NOT call any tool for rankings, top-N, country breakdowns,
   or cross-record summaries — no tool supports aggregation.

Do not invent IDs or values not present in the message.
Use "organization_id" as the literal placeholder for the org ID — it will be
replaced automatically before the call is made.
""".strip()

# ── Synthesizer prompt ────────────────────────────────────────────────────────
_SYNTHESIZER_PROMPT = """
You are a helpful operations assistant for a B2B merchant-export company.
You have been given the raw result of a Zoho database lookup.

Answer the user's question clearly and concisely using ONLY the data provided.

Formatting rules:
- Monetary values: Indian Rupees (₹) with Indian comma formatting (e.g. ₹3,40,000).
- Dates: DD Mon YYYY (e.g. 12 Jun 2026).
- If result has multiple records, summarise with key fields — do not dump raw JSON.
- If result contains no records, say so clearly (e.g. "You have no unpaid invoices.").
- Do not mention Zoho, MCP, tool names, or internal numeric IDs in your answer.
- Keep the answer short — 1–5 sentences for most queries.
""".strip()

# ── Schema cache (module-level, shared across requests) ───────────────────────
_tool_cache: list[dict] = []
_tool_cache_ts: float   = 0.0


async def _get_tools() -> list[dict]:
    """Return sanitised Groq tool schemas, refreshing from MCP when stale."""
    global _tool_cache, _tool_cache_ts
    if _tool_cache and (time.time() - _tool_cache_ts) < TOOL_CACHE_TTL:
        return _tool_cache
    async with ZohoMCPClient() as zoho:
        mcp_tools = await zoho.list_tools()
    _tool_cache    = tools_to_groq_schema(mcp_tools)
    _tool_cache_ts = time.time()
    log.info("[agent] tool cache refreshed — %d tools", len(_tool_cache))
    return _tool_cache


# ── Helpers ───────────────────────────────────────────────────────────────────

def _inject_org_id(args: dict, org_id: str) -> dict:
    """
    Replace the literal placeholder "organization_id" with the real org ID
    at any nesting level in the tool arguments.

    The routing prompt tells the model to use "organization_id" as a placeholder,
    so this is always a reliable substitution rather than a guess.
    """
    def _walk(obj):
        if isinstance(obj, dict):
            return {
                k: (org_id if (k == "organization_id" and obj[k] == "organization_id")
                    else _walk(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_walk(i) for i in obj]
        return obj
    return _walk(args)


def _is_grounded(result_text: str) -> bool:
    """
    Return False only for hard error conditions:
      - Empty / blank result from MCP
      - Zoho API error (response code != 0)

    Empty-but-valid results (empty arrays) return True — the synthesizer
    is instructed to tell the user "no records found" in that case.
    """
    if not result_text or not result_text.strip():
        return False
    try:
        data = json.loads(result_text)
        if isinstance(data, dict) and data.get("code", 0) != 0:
            log.info("[agent] Zoho API error: code=%s msg=%s",
                     data.get("code"), data.get("message", ""))
            return False
    except json.JSONDecodeError:
        pass  # non-JSON non-empty result → treat as grounded
    return True


# ── Main entry point ──────────────────────────────────────────────────────────

async def run(message: str, history: list[dict]) -> Optional[str]:
    """
    Run the Zoho agent loop for one user message.

    Parameters
    ----------
    message : str
        The rewritten user message in English.
    history : list[dict]
        Conversation history as {"role": ..., "content": ...} dicts.

    Returns
    -------
    str   — synthesized answer ready to send to the user.
    None  — tool errored, no tool matched, or Zoho returned an error.
            The caller should route to "escalate".
    """
    client = Groq(api_key=GROQ_API_KEY)

    # ── Step 1: fetch tool schemas (cached) ───────────────────────────────────
    try:
        tools = await _get_tools()
    except Exception as exc:
        log.error("[agent] failed to fetch tools: %s", exc)
        return None

    # ── Step 2: LLM picks a tool ──────────────────────────────────────────────
    recent = history[-6:] if len(history) > 6 else history
    messages = [{"role": "system", "content": _ROUTING_PROMPT}]
    messages.extend(recent)
    messages.append({"role": "user", "content": message})

    try:
        pick = client.chat.completions.create(
            model=AGENT_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0,
            max_tokens=512,
        )
    except Exception as exc:
        log.error("[agent] tool selection failed: %s", exc)
        return None

    calls = pick.choices[0].message.tool_calls or []
    if not calls:
        log.info("[agent] no tool selected — message will escalate")
        return None

    tool_name = calls[0].function.name
    try:
        tool_args = json.loads(calls[0].function.arguments)
    except (json.JSONDecodeError, TypeError):
        tool_args = {}

    log.info("[agent] selected tool=%s args=%s", tool_name, str(tool_args)[:160])

    # ── Step 3: inject real org_id ────────────────────────────────────────────
    if ZOHO_ORG_ID:
        tool_args = _inject_org_id(tool_args, ZOHO_ORG_ID)
        log.info("[agent] args after org_id injection: %s", str(tool_args)[:160])

    # ── Step 4: call Zoho ─────────────────────────────────────────────────────
    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool(tool_name, tool_args)
        result_text = result_to_text(result)
        log.info("[agent] tool result (first 300): %s", result_text[:300])
    except Exception as exc:
        log.error("[agent] tool execution failed: %s", exc)
        return None

    # ── Step 5: grounding check ───────────────────────────────────────────────
    if not _is_grounded(result_text):
        log.info("[agent] grounding failed — routing to escalate")
        return None

    # ── Step 6: synthesize ────────────────────────────────────────────────────
    synth_messages = [
        {"role": "system", "content": _SYNTHESIZER_PROMPT},
        {"role": "user",   "content": f"User asked: {message}\n\nZoho data:\n{result_text}"},
    ]
    try:
        synth = client.chat.completions.create(
            model=AGENT_MODEL,
            messages=synth_messages,
            temperature=0.1,
            max_tokens=512,
        )
        answer = synth.choices[0].message.content.strip()
        log.info("[agent] answer: %s", answer[:200])
        return answer
    except Exception as exc:
        log.error("[agent] synthesis failed: %s", exc)
        return None