# backend/zoho_mcp/write_agent.py
"""
Phase 3 — Write agent.

Responsible for:
  1. Generating a proposed write action (tool + args + plain-language proposal)
     from a user's message — WITHOUT executing anything.
  2. Executing a confirmed write action against the Zoho write MCP server.

Tool names verified against smoke_test list on write server.
Schema structure (from full schema inspection):
  - Both Books write tools require: body (record data) + query_params (org settings)
  - body.customer_id  — REQUIRED, numeric Zoho Books contact_id
  - body.line_items   — REQUIRED for estimates (array of {name,quantity,rate,unit})
  - query_params.send — MUST be "false" for estimates to prevent auto-sending
  - All placeholders injected at execute time: customer_id and organization_id
"""
import json
import logging
import time
from typing import Optional

from groq import Groq

from zoho_mcp.client import ZohoMCPClient, result_to_text, tools_to_groq_schema
from zoho_mcp.config import (
    GROQ_API_KEY, AGENT_MODEL, ZOHO_ORG_ID, TOOL_CACHE_TTL,
    ZOHO_WRITE_MCP_URL,
)
from zoho_mcp.identity import CustomerIdentity

log = logging.getLogger(__name__)

# ── Tool risk classification (verified tool names) ────────────────────────────
LOW_RISK_TOOLS: frozenset[str] = frozenset({
    "ZohoCRM_createRecords",     # Create a CRM Lead / Contact
    "ZohoCRM_updateRecord",
    "ZohoBooks_create_estimate", # Create a draft estimate (NOT sent)
})
HIGH_RISK_TOOLS: frozenset[str] = frozenset({
    "ZohoBooks_create_sales_order",  # Create a confirmed sales order
})
ALL_WRITE_TOOLS: frozenset[str] = LOW_RISK_TOOLS | HIGH_RISK_TOOLS


def classify_risk(tool_name: str, account_name: Optional[str] = None) -> str:
    """
    Return the effective risk level for this tool-account combination.

    Phase 6 addition: high-risk tools that have accumulated
    GRADUATION_THRESHOLD consecutive clean approvals are promoted to
    'low' risk and execute without operator approval.

    Lookup order:
      1. Hardcoded LOW_RISK_TOOLS → always "low"
      2. Hardcoded HIGH_RISK_TOOLS → check trust_levels table
         a. Specific account match → use that risk_level
         b. Wildcard match → use that risk_level
         c. No entry → default "high"
      3. Unknown tool → "unknown" (treated as "high" by callers)
    """
    if tool_name in LOW_RISK_TOOLS:
        return "low"
    if tool_name in HIGH_RISK_TOOLS:
        try:
            from zoho_mcp.learning import get_effective_risk
            return get_effective_risk(tool_name, account_name)
        except Exception as exc:
            log.warning("[write_agent] graduation check failed: %s — defaulting to high", exc)
            return "high"
    return "unknown"


# ── Write tool schema cache ───────────────────────────────────────────────────
_write_tool_cache:    list[dict] = []
_write_tool_cache_ts: float      = 0.0


async def _get_write_tools() -> list[dict]:
    global _write_tool_cache, _write_tool_cache_ts
    if _write_tool_cache and (time.time() - _write_tool_cache_ts) < TOOL_CACHE_TTL:
        return _write_tool_cache
    async with ZohoMCPClient(url=ZOHO_WRITE_MCP_URL) as zoho:
        mcp_tools = await zoho.list_tools()
    _write_tool_cache    = tools_to_groq_schema(mcp_tools)
    _write_tool_cache_ts = time.time()
    log.info("[write_agent] tool cache refreshed — %d tools", len(_write_tool_cache))
    return _write_tool_cache


# ── Prompts ───────────────────────────────────────────────────────────────────

def _proposal_system(identity: CustomerIdentity) -> str:
    account = identity.account_name or "the customer"
    contact = identity.contact_name or "the customer"
    has_books_id = bool(identity.books_customer_id)
    books_note   = (
        "NOTE: The Books customer_id is available for this customer."
        if has_books_id else
        "NOTE: Books customer_id is not yet resolved. Only ZohoCRM_createRecords is available."
    )
    return f"""
You are a write agent for a B2B merchant-export company.
The authenticated user is {contact} from {account}.

Given the user's request, output ONLY a JSON object with these keys:
{{
  "tool_name": "<exact tool name from the list>",
  "tool_args": {{ ... }},
  "proposal_text": "I'll [plain-language action]. Shall I proceed?"
}}

SCHEMA — every write tool uses body + query_params:
{{
  "body": {{
    "customer_id": "customer_id",
    ...record fields...
  }},
  "query_params": {{
    "organization_id": "organization_id"
    ...flags...
  }}
}}

PLACEHOLDERS (auto-replaced before execution — use literally):
  body.customer_id         → use the string "customer_id"
  query_params.organization_id → use the string "organization_id"

ESTIMATE RULES (ZohoBooks_create_estimate):
  - body.line_items is REQUIRED. Extract name, quantity, rate, unit from the message.
  - body.line_items format: [{{"name":"...", "quantity":0, "rate":0, "unit":"..."}}]
  - query_params.send MUST be "false" — NEVER auto-send an estimate to the customer.
  - quantity and rate should be numbers (not strings).
  - Example line item: {{"name":"Basmati Rice 25kg","quantity":100,"rate":2800,"unit":"Bags"}}

SALES ORDER RULES (ZohoBooks_create_sales_order):
  - body.customer_id is REQUIRED.
  - body.line_items is strongly recommended.
  - This is HIGH RISK and will require human approval.
  
CRM UPDATE RULES (ZohoCRM_updateRecord):
  - path_variables.module: Leads | Contacts | Deals | Accounts
  - path_variables.id: use "record_id" as placeholder
  - body.data: ONE object with:
      "_search_name": name to search for (resolves to record ID)
      + the fields to update
  - Lead_Status values: "New" | "Contacted" | "Qualified" |
    "Lost Lead" | "Not Contacted" | "Pre-Qualified"
  - Example: {{"path_variables": {{"module": "Leads", "id": "record_id"}},
               "body": {{"data": [{{"_search_name": "Rajesh Kumar",
                                  "Lead_Status": "Qualified"}}]}}}}

CRM LEAD RULES (ZohoCRM_createRecords):
  - path_variables.module must be "Leads".
  - body.data must be an array: [{{"Last_Name":"...", "Company":"...", "Phone":"..."}}]
  - Use the customer's details from the conversation.

{books_note}

proposal_text must be 1-2 friendly sentences summarising what will happen.
If no write tool fits, output: {{"tool_name": null, "tool_args": {{}}, "proposal_text": ""}}
Output ONLY valid JSON. No markdown, no explanation.
""".strip()


# ── Proposal generation ───────────────────────────────────────────────────────

async def generate_proposal(
    message:  str,
    history:  list[dict],
    identity: CustomerIdentity,
) -> Optional[tuple[str, str, dict, str]]:
    """
    Generate a write proposal without executing anything.
    Returns (tool_name, proposal_text, tool_args, risk) or None.
    """
    if not ZOHO_WRITE_MCP_URL:
        log.error("[write_agent] ZOHO_WRITE_MCP_URL not set")
        return None

    # For Books write tools we need the customer_id.
    # If it's missing, we can still create a CRM lead (doesn't need it).
    if not identity.books_customer_id:
        log.warning("[write_agent] no books_customer_id for %s — Books writes unavailable",
                    identity.account_name)

    client = Groq(api_key=GROQ_API_KEY)
    try:
        tools = await _get_write_tools()
    except Exception as exc:
        log.error("[write_agent] failed to fetch write tools: %s", exc)
        return None

    # Filter out Books write tools if we don't have a customer_id
    if not identity.books_customer_id:
        tools = [t for t in tools
                 if t["function"]["name"] not in
                 {"ZohoBooks_create_estimate", "ZohoBooks_create_sales_order"}]

    system = _proposal_system(identity)
    recent = history[-4:] if len(history) > 4 else history
    messages = [{"role": "system", "content": system}]
    messages.extend(recent)
    messages.append({"role": "user", "content": message})

    try:
        resp = client.chat.completions.create(
            model=AGENT_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=600,
        )
        parsed = json.loads(resp.choices[0].message.content.strip())
    except Exception as exc:
        log.error("[write_agent] proposal generation failed: %s", exc)
        return None

    tool_name     = parsed.get("tool_name")
    tool_args     = parsed.get("tool_args", {})
    proposal_text = parsed.get("proposal_text", "").strip()

    if not tool_name or not proposal_text:
        log.info("[write_agent] no write tool for: %s", message[:80])
        return None
    if tool_name not in ALL_WRITE_TOOLS:
        log.warning("[write_agent] unknown tool '%s' — rejecting", tool_name)
        return None

    risk = classify_risk(tool_name, identity.account_name)

    # Hard guard: Books write tools require a resolved books_customer_id.
    # generate_proposal() uses JSON mode (not function calling), so filtering
    # the tool list in the prompt has no effect — the model picks a name from
    # the system prompt text and can still propose a Books tool even when
    # books_customer_id is None. Reject here unconditionally so execute_write
    # is never called with a placeholder customer_id.
    _books_write = {"ZohoBooks_create_estimate", "ZohoBooks_create_sales_order"}
    if tool_name in _books_write and not identity.books_customer_id:
        log.error(
            "[write_agent] '%s' proposed but books_customer_id is None for '%s' — rejecting",
            tool_name, identity.account_name,
        )
        return None
    
    # Phase B: resolve CRM record ID before generating proposal
    if tool_name == "ZohoCRM_updateRecord":
        import copy
        tool_args = copy.deepcopy(tool_args)
        pv        = tool_args.setdefault("path_variables", {})
        module    = pv.get("module", "Leads")
        data      = tool_args.get("body", {}).get("data", [{}])
        fields    = data[0] if data else {}
        search_name = fields.pop("_search_name", "")
        if not search_name:
            log.warning("[write_agent] updateRecord missing _search_name")
            return None
        record_id = await _resolve_crm_record_id(module, search_name)
        if not record_id:
            log.warning("[write_agent] could not find %s '%s'", module, search_name)
            return None
        pv["recordId"] = record_id

    log.info("[write_agent] proposal: tool=%s risk=%s", tool_name, risk)
    return tool_name, proposal_text, tool_args, risk


# ── Injection helpers ─────────────────────────────────────────────────────────

def _inject_placeholder(args: dict, key: str, value: str) -> dict:
    """
    Walk args recursively and replace any field named `key` whose current
    value is the literal string `key` (the placeholder) with `value`.
    """
    def _walk(obj):
        if isinstance(obj, dict):
            return {
                k: (value if (k == key and obj[k] == key) else _walk(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_walk(i) for i in obj]
        return obj
    return _walk(args)

async def _resolve_crm_record_id(module: str, name: str) -> Optional[str]:
    """Search CRM read server for a record by name, return its ID."""
    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool("ZohoCRM_searchRecords", {
                "path_variables": {"module": module},
                "query_params":   {"word": name},
            })
        data    = json.loads(result_to_text(result))
        records = data.get("data", [])
        if records:
            log.info("[write_agent] resolved %s '%s' → %s", module, name, records[0]["id"])
            return records[0]["id"]
    except Exception as exc:
        log.warning("[write_agent] CRM lookup failed: %s", exc)
    return None


# ── Execution ─────────────────────────────────────────────────────────────────

async def execute_write(
    tool_name:         str,
    tool_args:         dict,
    books_customer_id: Optional[str] = None,
) -> Optional[str]:
    """
    Execute a confirmed write action against the Zoho write MCP server.
    Injects org_id and books_customer_id before calling.
    Returns result text on success, or None on any failure.

    Failure cases detected:
      - ZOHO_WRITE_MCP_URL not configured
      - MCP transport / network exception
      - Plain-text Zoho auth error ("Connection not authorised")
      - JSON Zoho API error (code != 0)
    """
    if not ZOHO_WRITE_MCP_URL:
        log.error("[write_agent] ZOHO_WRITE_MCP_URL not set")
        return None

    # Inject organization_id placeholder
    if ZOHO_ORG_ID:
        tool_args = _inject_placeholder(tool_args, "organization_id", ZOHO_ORG_ID)

    # Inject Books customer_id placeholder
    if books_customer_id:
        tool_args = _inject_placeholder(tool_args, "customer_id", books_customer_id)

    log.info("[write_agent] executing %s | args: %s", tool_name, str(tool_args)[:300])

    try:
        async with ZohoMCPClient(url=ZOHO_WRITE_MCP_URL) as zoho:
            result = await zoho.call_tool(tool_name, tool_args)
        result_text = result_to_text(result)
        log.info("[write_agent] result (first 300): %s", result_text[:300])
    except Exception as exc:
        log.error("[write_agent] execution failed: %s", exc)
        return None

    # ── Plain-text Zoho auth / permission errors ──────────────────────────────
    # These arrive as unstructured text (not JSON) so json.loads would silently
    # pass through them as "success". Catch them explicitly first.
    _lower = result_text.lower()
    if (
        "not authorised"   in _lower
        or "not authorized" in _lower
        or "cannot perform" in _lower
        or "unauthorized"   in _lower
    ):
        log.error("[write_agent] Zoho auth/permission error: %s", result_text[:200])
        return None

    # ── JSON Zoho API errors (code != 0) ─────────────────────────────────────
    try:
        data = json.loads(result_text)
        if isinstance(data, dict) and data.get("code", 0) != 0:
            log.error("[write_agent] Zoho error: code=%s msg=%s",
                      data.get("code"), data.get("message"))
            return None
    except json.JSONDecodeError:
        pass   # non-JSON non-error response — treat as success

    return result_text


# ── Retry wrapper ─────────────────────────────────────────────────────────────

async def execute_with_retry(
    tool_name:         str,
    tool_args:         dict,
    books_customer_id: Optional[str] = None,
    max_attempts:      int            = 3,
) -> Optional[str]:
    """
    Execute a write with exponential-backoff retry for transient failures.

    Attempt delays: 0s → 1s → 4s (2^0, 2^1 seconds between attempts).
    Only retries on None returns (network / Zoho errors). Returns immediately
    on success. After all attempts fail, returns None → caller escalates.

    Use this instead of execute_write() in main.py so a brief Zoho
    rate-limit or timeout doesn't lose the customer's confirmed action.
    """
    import asyncio
    for attempt in range(max_attempts):
        result = await execute_write(tool_name, tool_args, books_customer_id)
        if result is not None:
            return result
        if attempt < max_attempts - 1:
            wait = 2 ** attempt   # 1s, then 4s
            log.warning(
                "[write_agent] attempt %d/%d failed for %s — retrying in %ds",
                attempt + 1, max_attempts, tool_name, wait,
            )
            await asyncio.sleep(wait)
    log.error("[write_agent] all %d attempts failed for %s", max_attempts, tool_name)
    return None