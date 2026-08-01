# backend/zoho_mcp/crm_intelligence.py
"""
Phase F — CRM Intelligence.

Proactively surfaces pipeline health issues and enriches customer
conversations with relevant deal context.

Three capabilities:

  get_pipeline_alerts()       → list[str]
    Called from the digest scheduler. Returns alert strings for:
      - Open deals not updated in 14+ days (stale pipeline)
      - New leads not contacted in 48+ hours (response time)
      - Deals with closing date in the next 7 days (urgency)

  get_churn_risks()           → list[str]
    Called from the digest scheduler. Returns alert strings for
    CRM accounts with no record update in 90+ days (at-risk customers).

  get_customer_context(account_name) → str | None
    Called in main.py for every known-customer message. Returns a 1-2
    sentence context string about the customer's open deals that gets
    injected into the read agent's system prompt. Adds ~500ms latency
    but makes the bot context-aware (e.g., "I see you have an open deal
    at Negotiation stage — is this related to that?").

All functions use ZohoCRM_searchRecords on the read server (admin-authorized).
All functions are async and fail silently — any Zoho error returns [] or None
so the digest/agent are never blocked by intelligence fetch failures.
"""
import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from zoho_mcp.client import ZohoMCPClient, result_to_text

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

STALE_DEAL_DAYS    = 14   # deals not updated in this many days → stale
LEAD_FOLLOWUP_HRS  = 48   # new leads not contacted within this many hours
CHURN_RISK_DAYS    = 90   # accounts not updated in this many days → at risk
CLOSING_SOON_DAYS  = 7    # deals closing within this many days → urgent


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _search_crm(module: str, criteria: str, limit: int = 5) -> list[dict]:
    """Search a CRM module with criteria. Returns records or [] on failure."""
    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool("ZohoCRM_searchRecords", {
                "path_variables": {"module": module},
                "query_params":   {"criteria": criteria},
            })
        text    = result_to_text(result)
        data    = json.loads(text)
        records = data.get("data", [])
        log.info("[crm_intelligence] context fetch for '%s' → %d deals",
                 account_name, len(records))
        log.info("[crm_intelligence] %s '%s' → %d records",
                 module, criteria[:60], len(records))
        return records[:limit]
    except Exception as exc:
        log.warning("[crm_intelligence] search failed (%s): %s", module, exc)
        return []


# ── Pipeline health alerts ────────────────────────────────────────────────────

async def get_uncontacted_leads() -> list[dict]:
    """New leads created 48+ hours ago with no follow-up (still New status)."""
    cutoff = (datetime.now() - timedelta(hours=LEAD_FOLLOWUP_HRS)).strftime(
        "%Y-%m-%dT%H:%M:%S+05:30"
    )
    criteria = f"(Lead_Status:equals:New)AND(Created_Time:before:{cutoff})"
    return await _search_crm("Leads", criteria)

async def _get_all_open_deals(limit: int = 20) -> list[dict]:
    """Fetch open deals across all non-closed stages."""
    OPEN_STAGES = [
        "Proposal/Price Quote",
        "Negotiation/Review",
        "Value Proposition",
        "Qualification",
        "Needs Analysis",
    ]
    all_deals: list[dict] = []
    for stage in OPEN_STAGES:
        try:
            async with ZohoMCPClient() as zoho:
                result = await zoho.call_tool("ZohoCRM_searchRecords", {
                    "path_variables": {"module": "Deals"},
                    "query_params":   {"criteria": f"(Stage:equals:{stage})"},
                })
            data    = json.loads(result_to_text(result))
            all_deals.extend(data.get("data", []))
        except Exception:
            pass
    log.info("[crm_intelligence] open deals fetched: %d total", len(all_deals))
    return all_deals[:limit]


async def get_stale_deals() -> list[dict]:
    """Open deals with no update in STALE_DEAL_DAYS days."""
    cutoff  = datetime.now() - timedelta(days=STALE_DEAL_DAYS)
    deals   = await _get_all_open_deals()
    stale   = []
    for d in deals:
        modified_str = d.get("Modified_Time", "")[:10]   # YYYY-MM-DD
        if modified_str:
            try:
                modified = datetime.strptime(modified_str, "%Y-%m-%d")
                if modified < cutoff:
                    stale.append(d)
            except ValueError:
                pass
    return stale[:5]


async def get_deals_closing_soon() -> list[dict]:
    """Open deals with closing date in the next CLOSING_SOON_DAYS days."""
    today    = date.today()
    week_end = today + timedelta(days=CLOSING_SOON_DAYS)
    deals    = await _get_all_open_deals()
    closing  = []
    for d in deals:
        close_str = d.get("Closing_Date", "")
        if close_str:
            try:
                close_dt = date.fromisoformat(close_str[:10])
                if today <= close_dt <= week_end:
                    closing.append(d)
            except ValueError:
                pass
    return closing[:5]

async def get_pipeline_alerts() -> list[str]:
    """
    Aggregate pipeline health alerts for the morning digest.
    Returns a list of WhatsApp-formatted alert strings.
    """
    import asyncio
    stale, uncontacted, closing = await asyncio.gather(
        get_stale_deals(),
        get_uncontacted_leads(),
        get_deals_closing_soon(),
        return_exceptions=True,
    )

    alerts: list[str] = []

    stale = [] if isinstance(stale, Exception) else stale
    for d in stale:
        name    = d.get("Deal_Name", "Unknown deal")
        account = (d.get("Account_Name") or {}).get("name", "") if isinstance(
            d.get("Account_Name"), dict) else d.get("Account_Name", "")
        alerts.append(f"🔴 Stale deal ({STALE_DEAL_DAYS}+ days): {name}"
                      + (f" ({account})" if account else ""))

    uncontacted = [] if isinstance(uncontacted, Exception) else uncontacted
    for lead in uncontacted:
        name    = f"{lead.get('First_Name', '')} {lead.get('Last_Name', '')}".strip()
        company = lead.get("Company", "")
        alerts.append(f"🟡 Uncontacted lead (48h+): {name}"
                      + (f" — {company}" if company else ""))

    closing = [] if isinstance(closing, Exception) else closing
    for d in closing:
        name     = d.get("Deal_Name", "Unknown deal")
        close_dt = d.get("Closing_Date", "")
        alerts.append(f"📅 Deal closing soon: {name}"
                      + (f" (by {close_dt})" if close_dt else ""))

    return alerts


# ── Churn risk detection ──────────────────────────────────────────────────────

async def get_churn_risks() -> list[str]:
    """
    Accounts not updated in 90+ days — potential churn / inactive customers.
    Returns formatted alert strings.
    """
    cutoff   = (datetime.now() - timedelta(days=CHURN_RISK_DAYS)).strftime("%Y-%m-%d")
    criteria = f"(Modified_Time:before:{cutoff})"
    accounts = await _search_crm("Accounts", criteria, limit=5)

    alerts: list[str] = []
    for acct in accounts:
        name     = acct.get("Account_Name", "Unknown account")
        modified = acct.get("Modified_Time", "")[:10]   # YYYY-MM-DD
        alerts.append(f"⚠️ Inactive account ({CHURN_RISK_DAYS}+ days): {name}"
                      + (f" (last updated {modified})" if modified else ""))
    return alerts


# ── Customer context enrichment ───────────────────────────────────────────────

async def get_customer_context(account_name: str) -> Optional[str]:
    if not account_name:
        return None
    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool("ZohoCRM_searchRecords", {
                "path_variables": {"module": "Deals"},
                "query_params":   {"word": account_name},    # ← word search, not criteria
            })
        data    = json.loads(result_to_text(result))
        records = data.get("data", [])
        log.info("[crm_intelligence] context fetch for '%s' → %d deals",
                 account_name, len(records))
    except Exception as exc:
        log.warning("[crm_intelligence] customer context failed: %s", exc)
        return None

    if not records:
        return None

    open_deals   = [d for d in records
                    if d.get("Stage") not in ("Closed Won", "Closed Lost")]
    deals_to_use = open_deals[:3] if open_deals else records[:1]
    # ... rest unchanged

    parts: list[str] = []
    for d in deals_to_use:
        deal_name = d.get("Deal_Name", "Deal")
        stage     = d.get("Stage", "unknown stage")
        amount    = d.get("Amount", 0)
        close_dt  = d.get("Closing_Date", "")
        amount_str = f"₹{amount:,.0f}" if amount else ""
        line = f"'{deal_name}' at {stage}"
        if amount_str:
            line += f" ({amount_str})"
        if close_dt:
            line += f", closing {close_dt}"
        parts.append(line)

    n     = len(deals_to_use)
    intro = f"{account_name} has {n} deal{'s' if n > 1 else ''}: "
    return intro + "; ".join(parts) + "."


# ── Digest section formatter ──────────────────────────────────────────────────

async def get_intelligence_digest_section() -> str:
    """
    Build the CRM Intelligence section of the morning digest.
    Combines pipeline alerts and churn risks into a formatted string.
    Returns "" if nothing to report (no alerts, no at-risk accounts).
    """
    import asyncio
    pipeline_alerts, churn_alerts = await asyncio.gather(
        get_pipeline_alerts(),
        get_churn_risks(),
        return_exceptions=True,
    )

    pipeline_alerts = [] if isinstance(pipeline_alerts, Exception) else pipeline_alerts
    churn_alerts    = [] if isinstance(churn_alerts, Exception)    else churn_alerts

    if not pipeline_alerts and not churn_alerts:
        return ""

    lines: list[str] = [""]
    if pipeline_alerts:
        lines.append("*📊 Pipeline Intelligence:*")
        for a in pipeline_alerts:
            lines.append(f"  {a}")
    if churn_alerts:
        lines.append("*🔍 At-Risk Accounts:*")
        for a in churn_alerts:
            lines.append(f"  {a}")

    return "\n".join(lines)