# backend/zoho_mcp/deal_sync.py
"""
Phase C — CRM Deal sync.

Creates a Zoho CRM Deal every time a Books estimate is created, and
advances the deal's Stage field as the Quote-to-Order workflow progresses.

Stage mapping (configurable via env vars):
  Estimate created      → CRM_STAGE_QUOTATION  (default "Proposal/Price Quote")
  Customer accepts SO   → CRM_STAGE_COMMITTED  (default "Negotiation/Review")
  SO approved (Closed)  → CRM_STAGE_WON        (default "Closed Won")
  Workflow failed/cancel → CRM_STAGE_LOST       (default "Closed Lost")

Both create and update operations use the READ server (admin-authorized)
because Zoho CRM's API access (Crm_Implied_Api_Access) requires an admin
OAuth token — it cannot be granted to restricted users via profile settings.

The deal_id is stored in the workflow context so later stage updates
can reference the correct CRM record without an additional search.
"""
import json
import logging
import os
from datetime import date, timedelta
from typing import Optional

from zoho_mcp.client import ZohoMCPClient, result_to_text

log = logging.getLogger(__name__)

# ── Stage names — adjust to match your Zoho CRM pipeline config ──────────────
DEAL_STAGE_QUOTATION = os.getenv("CRM_STAGE_QUOTATION", "Proposal/Price Quote")
DEAL_STAGE_COMMITTED = os.getenv("CRM_STAGE_COMMITTED", "Negotiation/Review")
DEAL_STAGE_WON       = os.getenv("CRM_STAGE_WON",       "Closed Won")
DEAL_STAGE_LOST      = os.getenv("CRM_STAGE_LOST",      "Closed Lost")

# Default close date: 30 days from estimate creation
DEAL_CLOSE_DAYS = int(os.getenv("DEAL_CLOSE_DAYS", "30"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_from_estimate(result_text: str) -> tuple[str, str, float]:
    """
    Parse estimate number, estimate ID, and total from a Zoho Books
    create_estimate response.
    Returns (estimate_number, estimate_id, total).
    """
    try:
        data     = json.loads(result_text)
        estimate = data.get("estimate", {})
        number   = estimate.get("estimate_number", "")
        est_id   = estimate.get("estimate_id", "")
        total    = float(
            estimate.get("total")
            or estimate.get("sub_total")
            or 0
        )
        return number, est_id, total
    except Exception as exc:
        log.warning("[deal_sync] could not parse estimate response: %s", exc)
        return "", "", 0.0


def _close_date() -> str:
    """Return a closing date N days from today in YYYY-MM-DD format."""
    return (date.today() + timedelta(days=DEAL_CLOSE_DAYS)).isoformat()


# ── CRM Deal creation ─────────────────────────────────────────────────────────

async def create_from_estimate(
    account_name: str,
    result_text:  str,
) -> Optional[str]:
    """
    Create a CRM Deal linked to a newly created Books estimate.

    Called immediately after ZohoBooks_create_estimate succeeds.
    The deal_id returned should be stored in the workflow context
    so stage updates can reference it later.

    Returns the CRM Deal record ID, or None on failure.
    """
    estimate_number, estimate_id, total = _extract_from_estimate(result_text)

    if not estimate_number:
        log.warning("[deal_sync] no estimate_number in response — skipping deal creation")
        return None

    deal_name = f"{estimate_number} — {account_name}"

    deal_data = {
        "Deal_Name":    deal_name,
        "Stage":        DEAL_STAGE_QUOTATION,
        "Account_Name": account_name,
        "Closing_Date": _close_date(),
        "Description":  f"Linked to Books estimate {estimate_number} ({estimate_id})",
    }

    # Only include Amount if we successfully parsed a non-zero value
    if total > 0:
        deal_data["Amount"] = total

    try:
        async with ZohoMCPClient() as zoho:    # read server — admin-authorized
            result = await zoho.call_tool("ZohoCRM_createRecords", {
                "path_variables": {"module": "Deals"},
                "body":           {"data": [deal_data]},
            })
        text    = result_to_text(result)
        log.info("[deal_sync] CRM response (first 200): %s", text[:200])

        data    = json.loads(text)
        records = data.get("data", [])
        if records and records[0].get("code") == "SUCCESS":
            deal_id = records[0].get("details", {}).get("id", "")
            log.info(
                "[deal_sync] Deal created id=%s name='%s' stage='%s' amount=%.2f",
                deal_id, deal_name, DEAL_STAGE_QUOTATION, total,
            )
            return deal_id
        else:
            log.error("[deal_sync] Zoho rejected deal creation: %s", text[:300])
            return None

    except Exception as exc:
        log.error("[deal_sync] create_from_estimate failed: %s", exc)
        return None


# ── CRM Deal stage update ─────────────────────────────────────────────────────

async def advance_stage(deal_id: str, stage: str) -> bool:
    """
    Update a CRM Deal's Stage field.

    Called when the Books workflow advances:
      customer accepts    → DEAL_STAGE_COMMITTED
      SO approved         → DEAL_STAGE_WON
      cancelled/failed    → DEAL_STAGE_LOST

    Returns True on success, False on any error.
    """
    if not deal_id:
        log.warning("[deal_sync] advance_stage called with empty deal_id — skipping")
        return False

    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool("ZohoCRM_updateRecord", {
                "path_variables": {"module": "Deals", "recordId": deal_id},
                "body":           {"data": [{"Stage": stage}]},
            })
        text = result_to_text(result)
        log.info("[deal_sync] Stage update response (first 200): %s", text[:200])

        data    = json.loads(text)
        records = data.get("data", [])
        if records and records[0].get("code") in ("SUCCESS", "UPDATED"):
            log.info("[deal_sync] Deal %s stage → '%s'", deal_id, stage)  # ← move here
            return True
        else:
            log.error("[deal_sync] stage update failed: %s", text[:200])
            return False

    except Exception as exc:
        log.error("[deal_sync] advance_stage failed: %s", exc)
        return False