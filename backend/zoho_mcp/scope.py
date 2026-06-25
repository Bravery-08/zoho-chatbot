# backend/zoho_mcp/scope.py
"""
Phase 2 — Per-customer data scoping and tool allowlisting.

This module is the trust boundary between the agent and Zoho's data.
It enforces two controls:

1. TOOL ALLOWLIST
   Customers only receive a filtered subset of tool schemas.
   Internal/staff receive all tools.
   This is structural — a customer-facing agent simply doesn't have the
   schema for ZohoCRM_searchRecords, so it can't be called even if the
   user asks for it or tries to inject it via the message.

2. CUSTOMER SCOPING
   For Books list tools: inject customer_name into query_params before
   the call. Even if the model generates args without a customer filter
   (or with someone else's name), this layer always overrides it.

   For Inventory get tools: fetch the record, then verify the
   customer_name field on the result matches the authenticated account.
   If it doesn't, the agent returns None → escalate.

Scoping happens in two places:
  - inject_customer_scope()  — called BEFORE the Zoho tool call
  - verify_result_ownership() — called AFTER the Zoho tool call

Both are called by agent.run() when identity.state == "known".
Neither is called for internal users (state == "internal").
"""
import copy
import json
import logging
from typing import Optional

log = logging.getLogger(__name__)


# ── Customer-facing tool allowlist ────────────────────────────────────────────
# These are the only tools a verified customer may invoke.
# Everything else — CRM search, analytics, stock levels, purchase orders —
# is internal-only and structurally unavailable to customer sessions.
CUSTOMER_TOOLS: frozenset[str] = frozenset({
    "ZohoBooks_list_invoices",        # own invoices only
    "ZohoBooks_list_estimates",       # own estimates only
    "ZohoBooks_list_sales_orders",    # own sales orders only (Books view)
    "ZohoInventory_get_sales_order",  # own SO only (verified post-fetch)
    "ZohoInventory_get_shipment_order",  # own shipment only (verified post-fetch)
})

# Books list tools that accept a customer_name query param for filtering.
# The value of each entry is the exact query_params key Zoho expects.
_INJECT_BY_NAME: dict[str, str] = {
    "ZohoBooks_list_invoices":     "customer_name",
    "ZohoBooks_list_estimates":    "customer_name",
    "ZohoBooks_list_sales_orders": "customer_name",
}

# Inventory get-by-ID tools that need post-fetch ownership verification.
_VERIFY_OWNERSHIP: frozenset[str] = frozenset({
    "ZohoInventory_get_sales_order",
    "ZohoInventory_get_shipment_order",
})


# ── Tool filtering ────────────────────────────────────────────────────────────

def filter_tools(all_tools: list[dict], state: str) -> list[dict]:
    """
    Return the subset of tool schemas available to this identity state.

      internal → all tools (staff, no restrictions)
      known    → CUSTOMER_TOOLS only
      unknown  → empty list (no Zoho data access)

    Filtering the schema list is structural security — it's not possible
    for a customer to call a tool that isn't in their schema list,
    regardless of what they write in their message.
    """
    if state == "internal":
        return all_tools
    if state == "known":
        allowed = [
            t for t in all_tools
            if t.get("function", {}).get("name") in CUSTOMER_TOOLS
        ]
        log.info("[scope] customer tool list: %d of %d tools",
                 len(allowed), len(all_tools))
        return allowed
    # unknown
    return []


# ── Pre-call injection ────────────────────────────────────────────────────────

def inject_customer_scope(
    tool_name:    str,
    args:         dict,
    account_name: Optional[str],
) -> dict:
    """
    Enforce customer scoping on Books list tools before the Zoho call.

    Injects the authenticated customer's account_name into query_params,
    always overriding whatever the model generated. This prevents:
      - Model forgetting to add a customer filter (returns all records)
      - Prompt injection that substitutes another customer's name
      - Model hallucinating a different customer_name

    For Inventory get tools, no pre-call injection is needed — scoping
    is done post-fetch by verify_result_ownership().

    Returns a deep-copied and scoped args dict.
    """
    if not account_name or tool_name not in _INJECT_BY_NAME:
        return args

    args = copy.deepcopy(args)
    qp   = args.setdefault("query_params", {})
    field = _INJECT_BY_NAME[tool_name]

    model_value = qp.get(field)
    if model_value and model_value != account_name:
        log.warning(
            "[scope] injection override on %s: model wrote %s='%s', "
            "enforcing '%s'",
            tool_name, field, model_value, account_name,
        )

    qp[field] = account_name
    log.info("[scope] injected %s='%s' into %s args", field, account_name, tool_name)
    return args


# ── Post-fetch verification ───────────────────────────────────────────────────

def verify_result_ownership(
    tool_name:    str,
    result_text:  str,
    account_name: Optional[str],
) -> bool:
    """
    Verify a fetched Inventory record belongs to the authenticated customer.

    Only runs for tools in _VERIFY_OWNERSHIP. All other tools have already
    been scoped by inject_customer_scope() and don't need post-fetch checks.

    Returns True if:
      - tool is not a verify-type tool
      - account_name is not set (internal caller)
      - the record's customer_name matches account_name (case-insensitive)
      - result can't be parsed (allow-through to avoid false positives)

    Returns False if the record belongs to a different customer.
    The agent converts False → None → escalate.
    """
    if tool_name not in _VERIFY_OWNERSHIP:
        return True     # already scoped by injection, no verify needed
    if not account_name:
        return True     # internal caller, no restriction

    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        log.warning("[scope] %s: non-JSON result, cannot verify ownership — allowing",
                    tool_name)
        return True

    # Zoho Inventory wraps the record under the entity key
    record = (
        data.get("salesorder")
        or data.get("shipmentorder")
        or data.get("shipment_order")
        or {}
    )
    record_customer = record.get("customer_name", "")

    if not record_customer:
        # Record exists but has no customer_name field — allow through
        log.warning("[scope] %s: no customer_name in result, allowing", tool_name)
        return True

    if record_customer.lower() == account_name.lower():
        log.info("[scope] ownership verified: '%s' owns this %s", account_name, tool_name)
        return True

    log.warning(
        "[scope] OWNERSHIP MISMATCH on %s: authenticated='%s' record_owner='%s'",
        tool_name, account_name, record_customer,
    )
    return False