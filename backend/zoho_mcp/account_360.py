# backend/zoho_mcp/account_360.py
"""
Phase D — Account 360 view.

Fetches a complete picture of an account from multiple Zoho sources
in parallel and synthesises it into a single WhatsApp-formatted summary.

Sources read concurrently:
  ZohoCRM_searchRecords (Accounts)  — account details, industry, address
  ZohoCRM_searchRecords (Contacts)  — all contacts at the account
  ZohoCRM_searchRecords (Deals)     — open and recent deals
  ZohoBooks_list_invoices           — recent invoice history
  ZohoBooks_list_estimates          — recent estimate/quote history

Staff only — customers cannot request Account 360 views.
Triggered in main.py when identity.state == "internal" and the
query matches account-summary keywords.
"""
import asyncio
import json
import logging
import os
from typing import Optional

from groq import Groq

from zoho_mcp.client import ZohoMCPClient, result_to_text
from zoho_mcp.config import GROQ_API_KEY, AGENT_MODEL, ZOHO_ORG_ID

log = logging.getLogger(__name__)

# Trigger phrases — checked in main.py before normal intent routing
TRIGGER_PHRASES = (
    "360", "full history", "account summary", "account overview",
    "complete profile", "all info about", "everything about",
    "full overview", "tell me about", "brief me on",
)


def is_360_request(message: str) -> bool:
    """Return True if this looks like an Account 360 request."""
    lower = message.lower()
    return any(phrase in lower for phrase in TRIGGER_PHRASES)


async def extract_account_name(message: str) -> Optional[str]:
    """
    Use a fast LLM call to extract the company/account name from the query.
    Returns the name string, or None if no company name is found.
    """
    client = Groq(api_key=GROQ_API_KEY)
    try:
        resp = client.chat.completions.create(
            model=os.getenv("INTENT_MODEL", "llama-3.1-8b-instant"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract the company or account name from the message. "
                        "Output ONLY a JSON object: {\"account\": \"Company Name\"} "
                        "If no company name is found, output: {\"account\": null}"
                    ),
                },
                {"role": "user", "content": message},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=30,
        )
        data = json.loads(resp.choices[0].message.content)
        name = data.get("account")
        if name:
            log.info("[account_360] extracted account='%s'", name)
        return name
    except Exception as exc:
        log.warning("[account_360] name extraction failed: %s", exc)
        return None


# ── Individual data fetchers ──────────────────────────────────────────────────

async def _fetch_crm_account(account_name: str) -> dict:
    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool("ZohoCRM_searchRecords", {
                "path_variables": {"module": "Accounts"},
                "query_params":   {"word": account_name},
            })
        data    = json.loads(result_to_text(result))
        records = data.get("data", [])
        return records[0] if records else {}
    except Exception as exc:
        log.warning("[account_360] CRM account fetch failed: %s", exc)
        return {}


async def _fetch_crm_contacts(account_name: str) -> list:
    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool("ZohoCRM_searchRecords", {
                "path_variables": {"module": "Contacts"},
                "query_params":   {"word": account_name},
            })
        data = json.loads(result_to_text(result))
        return data.get("data", [])[:5]   # cap at 5 contacts
    except Exception as exc:
        log.warning("[account_360] CRM contacts fetch failed: %s", exc)
        return []


async def _fetch_crm_deals(account_name: str) -> list:
    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool("ZohoCRM_searchRecords", {
                "path_variables": {"module": "Deals"},
                "query_params":   {"word": account_name},
            })
        data = json.loads(result_to_text(result))
        return data.get("data", [])[:5]   # cap at 5 deals
    except Exception as exc:
        log.warning("[account_360] CRM deals fetch failed: %s", exc)
        return []


async def _fetch_books_invoices(account_name: str) -> list:
    if not ZOHO_ORG_ID:
        return []
    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool("ZohoBooks_list_invoices", {
                "query_params": {
                    "organization_id": ZOHO_ORG_ID,
                    "customer_name":   account_name,
                    "sort_column":     "date",
                    "sort_order":      "D",
                },
            })
        data = json.loads(result_to_text(result))
        return data.get("invoices", [])[:5]
    except Exception as exc:
        log.warning("[account_360] Books invoices fetch failed: %s", exc)
        return []


async def _fetch_books_estimates(account_name: str) -> list:
    if not ZOHO_ORG_ID:
        return []
    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool("ZohoBooks_list_estimates", {
                "query_params": {
                    "organization_id": ZOHO_ORG_ID,
                    "customer_name":   account_name,
                    "sort_column":     "date",
                    "sort_order":      "D",
                },
            })
        data = json.loads(result_to_text(result))
        return data.get("estimates", [])[:5]
    except Exception as exc:
        log.warning("[account_360] Books estimates fetch failed: %s", exc)
        return []


# ── Synthesis ─────────────────────────────────────────────────────────────────

async def _synthesize(account_name: str, context: dict) -> str:
    """Use Groq to turn the multi-source data into a readable WhatsApp summary."""
    client = Groq(api_key=GROQ_API_KEY)
    system = (
        "You are an operations assistant for a B2B merchant-export company. "
        "Summarise the account data below into a concise WhatsApp message. "
        "Use *bold* for section headers. "
        "Format monetary values as ₹ with Indian comma formatting. "
        "Keep it under 20 lines total. "
        "If a section has no data, omit it entirely."
    )
    user = (
        f"Account: {account_name}\n\n"
        f"Data:\n{json.dumps(context, indent=2, default=str)}"
    )
    try:
        resp = client.chat.completions.create(
            model=AGENT_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            temperature=0.1,
            max_tokens=600,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        log.error("[account_360] synthesis failed: %s", exc)
        return f"Could not generate 360 view for {account_name} — please try again."


# ── Main entry point ──────────────────────────────────────────────────────────

async def get_summary(account_name: str) -> str:
    """
    Fetch all account data in parallel and return a synthesised summary.

    Makes 5 concurrent Zoho API calls (2 CRM + 2 Books + 1 CRM Contacts)
    then synthesises the result with one Groq call.
    Total latency: max(individual call latency) + synthesis ≈ 3–6 seconds.
    """
    log.info("[account_360] fetching 360 view for '%s'", account_name)

    crm_account, crm_contacts, crm_deals, invoices, estimates = await asyncio.gather(
        _fetch_crm_account(account_name),
        _fetch_crm_contacts(account_name),
        _fetch_crm_deals(account_name),
        _fetch_books_invoices(account_name),
        _fetch_books_estimates(account_name),
        return_exceptions=True,
    )

    # Treat exceptions as empty results
    def _safe(val):
        return val if not isinstance(val, Exception) else []

    context = {
        "account_details": _safe(crm_account),
        "contacts":        _safe(crm_contacts),
        "deals":           _safe(crm_deals),
        "recent_invoices": _safe(invoices),
        "recent_estimates": _safe(estimates),
    }

    log.info(
        "[account_360] fetched: account=%s contacts=%d deals=%d "
        "invoices=%d estimates=%d",
        bool(context["account_details"]),
        len(context["contacts"]),
        len(context["deals"]),
        len(context["recent_invoices"]),
        len(context["recent_estimates"]),
    )

    return await _synthesize(account_name, context)