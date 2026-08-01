# backend/zoho_mcp/agent.py
"""
Phase 1 / Phase 2 Zoho agent loop.

Phase 1 flow (no identity):
  pick tool → inject org_id → call Zoho → ground → synthesize

Phase 2 additions (with identity):
  filter tools by identity state → inject org_id → inject customer scope →
  call Zoho → verify ownership → ground → synthesize
"""
import json
import logging
import re
import time
from typing import Optional

from groq import Groq, BadRequestError as GroqBadRequestError

from zoho_mcp.client import ZohoMCPClient, result_to_text, tools_to_groq_schema
from zoho_mcp.config import GROQ_API_KEY, AGENT_MODEL, ZOHO_ORG_ID, TOOL_CACHE_TTL
from zoho_mcp.identity import CustomerIdentity
import zoho_mcp.scope as scope

log = logging.getLogger(__name__)

_ROUTING_PROMPT_TEMPLATE="""
You are an operations agent for a B2B merchant-export company.
You have access ONLY to the Zoho business data tools provided — no search
engines, calculators, or any other tools. Never call a tool not in the list.

WHEN TO CALL A TOOL: only to look up live Zoho data (contacts, invoices,
orders, stock, shipments, balances). Do NOT call any tool for general
knowledge, "how to" questions, weather, exchange rates, or calculations.

ROUTING RULES — apply when two tools could both fit:

1. CRM LOOKUPS: use ZohoCRM_searchRecords for any search by name, company,
   location, stage, attribute, phone, or email across any CRM module (Leads,
   Contacts, Deals, Accounts, Activities, Tasks).
   Use ZohoCRM_getRecord only when you have an explicit numeric record ID.
   NEVER call ZohoBooks_list_contacts in response to any user-facing query.
   ZohoBooks_list_contacts is an internal system tool for customer ID
   resolution only — it must never be used to answer a contact search question
   and must never be called when the user asks about contacts, customers, or
   any business entity.

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

6. FILTERING vs AGGREGATION:
   Filtering records by stage, date, or status IS a valid tool call:
     "deals won this quarter"  → ZohoCRM_searchRecords (Deals, stage=Closed Won) ✓
     "leads from last week"    → ZohoCRM_searchRecords (Leads, by date) ✓
     "unpaid invoices"         → ZohoBooks_list_invoices ✓
   These require cross-record calculation — do NOT call any tool:
     "top 5 customers by revenue this quarter"  → no tool (ranks across records)
     "win rate this quarter"                    → no tool (percentage)
     "average order value"                      → no tool (arithmetic)
     "how many deals did we close"              → no tool (count across records)

7. EXTERNAL, COMPETITOR, AND GENERAL KNOWLEDGE:
   Do not call any tool for:
   • Competitors or external companies not in your Zoho:
       "which countries do competitors sell to"  → no tool
       "compare us to industry benchmarks"       → no tool
   • Trade term explanations: "what is CIF?", "explain FOB", "how does GST work?".
   • Any educational or concept explanation question.
   • Exchange rates, weather, or anything not stored inside your Zoho organisation.

8. WRITE OPERATIONS ON READ-ONLY TOOL LIST:
   If the user asks to CREATE, UPDATE, or DELETE a record (create invoice,
   send estimate, delete lead, make payment, raise a PO) and no write tool
   appears in the tool list provided, do not call any tool. Do not call a read
   tool as a substitute for a write operation.
   This includes activity and task creation:
     "Log a call with Rajesh..."         → no tool (write operation)
     "Remind me to follow up with..."    → no tool (write operation)
     "Create a meeting note for..."      → no tool (write operation)

9. CONVERSATIONAL MESSAGES:
   Do not call any tool for messages with no data query intent:
   greetings, acknowledgements, filler — "okay", "got it", "thanks", "bye",
   "I understand", "sounds good", "noted", "great". These are not Zoho queries.
   
10. CRM TASKS DATE SEARCH: when filtering Tasks by date, always use the field
    name "Due_Date" (underscore, not space). Never use today() — substitute
    the actual date in YYYY-MM-DD format.
    Today's date is {today}.
    Correct:   (Due_Date:equals:2026-07-31)
    Incorrect: (Due Date:equals:today())

Do not invent IDs or values not present in the message.
Use "organization_id" as a placeholder for the org ID — it is replaced
automatically before the call is executed.

WHEN IN DOUBT — DO NOT CALL ANY TOOL:
If the message does not clearly map to a specific Zoho data lookup, do not
call any tool. Examples of messages that must NOT trigger any tool call:

  "Who are our top 5 customers by revenue this quarter?"
      → no tool. Requires cross-record ranking — no tool supports this.

  "Which countries do most of our export competitors sell to?"
      → no tool. Competitors are not in Zoho; this is external market data.

  "What is the capital of Saudi Arabia?"
      → no tool. General knowledge question, not a Zoho data query.

  "Thanks, that is all for now."
      → no tool. Conversational acknowledgement with no data intent.

  "Okay, got it."
      → no tool. Conversational filler with no data intent.

  "Create an invoice for Nile Trading for $5000."
      → no tool. Write operation; no write tool in this tool list.
      
  "What is the current USD to INR exchange rate?"
      → no tool (external market data, not in Zoho)
      
  "Explain the difference between CIF and FOB pricing."
      → no tool. Trade term explanation, not a Zoho data query.

The rule: if you cannot identify a specific Zoho module and a specific record
or filter to retrieve, do not call any tool.

CRITICAL — SCHEMA STRUCTURE: Every Zoho tool wraps its parameters under
query_params and/or path_variables. Never generate flat top-level args.
CORRECT:   {{"query_params": {{"organization_id": "organization_id", "filter_by": "Status.Unpaid"}}}}
INCORRECT: {{"organization_id": "organization_id", "filter_by": "Status.Unpaid"}}
""".strip()

def _get_routing_prompt() -> str:
    from datetime import date
    today = date.today().isoformat()
    return _ROUTING_PROMPT_TEMPLATE.replace("{today}", today)

# ── Synthesizer prompt ────────────────────────────────────────────────────────
_SYNTHESIZER_PROMPT = """
You are a helpful operations assistant for a B2B merchant-export company.
Answer the user's question clearly and concisely using ONLY the data provided.

Rules:
- Monetary values: use ₹ with Indian formatting (₹3,40,000).
- Dates: DD Mon YYYY (e.g. 12 Jun 2026).
- Multiple records: summarise with key fields, do not dump raw JSON.
- No records found: say so clearly ("You have no unpaid invoices.").
- Never mention Zoho, MCP, tool names, or internal numeric IDs.
- Keep the answer to 1–5 sentences for most queries.
""".strip()

# ── Tool schema cache ─────────────────────────────────────────────────────────
_tool_cache:    list[dict] = []
_tool_cache_ts: float      = 0.0


# Tools that are internal-only and must never appear in the routing agent's schema.
# ZohoBooks_list_contacts is called directly by identity.py for customer_id lookup —
# it must not be selectable by the LLM for user queries.
_AGENT_EXCLUDED_TOOLS: frozenset[str] = frozenset({"ZohoBooks_list_contacts","ZohoCRM_createRecords","ZohoCRM_updateRecord",})


async def _get_tools() -> list[dict]:
    """Return sanitised Groq tool schemas, refreshing from MCP when stale."""
    global _tool_cache, _tool_cache_ts
    if _tool_cache and (time.time() - _tool_cache_ts) < TOOL_CACHE_TTL:
        return _tool_cache
    async with ZohoMCPClient() as zoho:
        mcp_tools = await zoho.list_tools()
    all_schemas   = tools_to_groq_schema(mcp_tools)
    _tool_cache    = [t for t in all_schemas
                      if t["function"]["name"] not in _AGENT_EXCLUDED_TOOLS]
    _tool_cache_ts = time.time()
    log.info("[agent] tool cache refreshed — %d tools (%d excluded)",
             len(_tool_cache), len(all_schemas) - len(_tool_cache))
    return _tool_cache


# ── 400 recovery helpers ──────────────────────────────────────────────────────

def _recover_from_400(error: GroqBadRequestError) -> tuple[str | None, dict]:
    """
    When Groq rejects a tool call (HTTP 400), extract the intended tool name
    and args from the failed_generation field in the error body.
    Same pattern as run_eval.py — reused verbatim for consistency.
    """
    try:
        body       = error.response.json()
        failed_gen = body.get("error", {}).get("failed_generation", "")
        match      = re.search(r"<function=([^>]+)>(.*?)</function>",
                               failed_gen, re.DOTALL)
        if match:
            tool_name = match.group(1).strip()
            try:
                args = json.loads(match.group(2).strip())
            except json.JSONDecodeError:
                args = {}
            return tool_name, args
    except Exception:
        pass
    return None, {}


def _fix_nesting(tool_name: str, args: dict) -> dict:
    """
    Auto-correct flat args that should be nested under query_params.

    The model occasionally generates:
        {"organization_id": "x", "customer_name": "y"}
    when the schema requires:
        {"query_params": {"organization_id": "x", "customer_name": "y"}}

    Detection: args have neither "query_params" nor "path_variables" keys,
    but the cached tool schema declares "query_params" as a required property.
    In that case, wrap the whole flat dict under query_params.
    """
    if "query_params" in args or "path_variables" in args:
        return args     # already correctly nested

    # Look up the schema from the module-level cache
    schema = next(
        (t["function"].get("parameters", {})
         for t in _tool_cache
         if t.get("function", {}).get("name") == tool_name),
        None,
    )
    if schema and "query_params" in schema.get("required", []):
        log.warning("[agent] %s: flat args detected — wrapping in query_params", tool_name)
        return {"query_params": args}

    return args


# ── org_id injection ──────────────────────────────────────────────────────────

def _inject_org_id(args: dict, org_id: str) -> dict:
    """Replace the literal placeholder 'organization_id' with the real org ID."""
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


# ── Grounding check ───────────────────────────────────────────────────────────

def _is_grounded(result_text: str) -> bool:
    """
    False only on hard error conditions (blank, or Zoho API error code != 0).
    Empty-but-valid results → True (synthesizer says "no records found").
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
        pass   # non-JSON non-empty → treat as grounded
    return True


# ── Main entry point ──────────────────────────────────────────────────────────

async def run(
    message:          str,
    history:          list[dict],
    identity:         Optional[CustomerIdentity] = None,
    customer_context: Optional[str]              = None,
) -> Optional[str]:
    """
    Run the Zoho agent loop for one user message.

    Parameters
    ----------
    message  : rewritten user message in English.
    history  : conversation history as {"role", "content"} dicts.
    identity : resolved CustomerIdentity from identity.resolve().
               None → treated as internal (Phase 1 backwards-compatibility).

    Returns
    -------
    str   — synthesized answer to send to the user.
    None  — tool errored, no tool matched, API error, or ownership check
            failed. Caller should route to "escalate".
    """
    client = Groq(api_key=GROQ_API_KEY)

    # ── Step 1: fetch + filter tool schemas ───────────────────────────────────
    try:
        all_tools = await _get_tools()
    except Exception as exc:
        log.error("[agent] failed to fetch tools: %s", exc)
        return None

    state = identity.state if identity else "internal"
    tools = scope.filter_tools(all_tools, state)

    if not tools:
        # unknown identity — no tools available
        log.info("[agent] no tools available for state=%s", state)
        return None

    # ── Step 2: LLM picks a tool ──────────────────────────────────────────────
    recent   = history[-6:] if len(history) > 6 else history
    messages = [{"role": "system", "content": _get_routing_prompt()}]
    # ── Phase F: inject customer context if available ─────────────────────────
    if customer_context:
        ctx_msg = (
            "CUSTOMER CONTEXT — use this to give more relevant answers "
            "and proactively reference open business when appropriate:\n"
            + customer_context
        )
        messages.append({"role": "system", "content": ctx_msg})
    messages.extend(recent)
    messages.append({"role": "user", "content": message})

    tool_name: str | None = None
    tool_args: dict       = {}

    try:
        pick  = client.chat.completions.create(
            model=AGENT_MODEL, messages=messages,
            tools=tools, tool_choice="auto",
            temperature=0, max_tokens=512,
        )
        calls = pick.choices[0].message.tool_calls or []
        if not calls:
            log.info("[agent] no tool selected — message will escalate")
            return None
        tool_name = calls[0].function.name
        try:
            tool_args = json.loads(calls[0].function.arguments)
        except (json.JSONDecodeError, TypeError):
            tool_args = {}

    except GroqBadRequestError as exc:
        # Groq rejected the call for schema violations (e.g. flat args instead
        # of nested query_params). Recover the intended tool from failed_generation
        # and fix the nesting before proceeding — same recovery as run_eval.py.
        tool_name, tool_args = _recover_from_400(exc)
        if not tool_name:
            log.error("[agent] 400 with no recoverable tool: %s", exc)
            return None
        log.warning("[agent] recovered from 400 — tool=%s (will fix nesting)", tool_name)
        tool_args = _fix_nesting(tool_name, tool_args)

    except Exception as exc:
        log.error("[agent] tool selection failed: %s", exc)
        return None

    log.info("[agent] selected tool=%s", tool_name)

    # ── Step 3: inject org_id and customer scope ──────────────────────────────
    if ZOHO_ORG_ID:
        tool_args = _inject_org_id(tool_args, ZOHO_ORG_ID)

    account_name = identity.account_name if identity else None
    tool_args = scope.inject_customer_scope(tool_name, tool_args, account_name)

    log.info("[agent] final args: %s", str(tool_args)[:200])

    # ── Step 4: call Zoho ─────────────────────────────────────────────────────
    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool(tool_name, tool_args)
        result_text = result_to_text(result)
        log.info("[agent] result (first 300): %s", result_text[:300])
    except Exception as exc:
        log.error("[agent] tool execution failed: %s", exc)
        return None

    # ── Step 5: grounding check ───────────────────────────────────────────────
    if not _is_grounded(result_text):
        log.info("[agent] grounding failed — routing to escalate")
        return None

    # ── Step 6: ownership verification (Inventory get tools) ─────────────────
    if not scope.verify_result_ownership(tool_name, result_text, account_name):
        log.warning("[agent] ownership check failed for %s — routing to escalate",
                    tool_name)
        return None

    # ── Step 7: synthesize ────────────────────────────────────────────────────
    synth_messages = [
        {"role": "system", "content": _SYNTHESIZER_PROMPT},
        {"role": "user",   "content": f"User asked: {message}\n\nZoho data:\n{result_text}"},
    ]
    try:
        synth  = client.chat.completions.create(
            model=AGENT_MODEL, messages=synth_messages,
            temperature=0.1, max_tokens=512,
        )
        answer = synth.choices[0].message.content.strip()
        log.info("[agent] answer: %s", answer[:200])
        return answer
    except Exception as exc:
        log.error("[agent] synthesis failed: %s", exc)
        return None