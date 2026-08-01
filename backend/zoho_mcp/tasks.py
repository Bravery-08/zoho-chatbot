# backend/zoho_mcp/tasks.py
"""
Phase E — CRM Tasks and Activities integration.

Provides two functions used by the daily digest and alert system:

  get_due_today_text()   — fetches Tasks due today from Zoho CRM,
                           returns a formatted WhatsApp string for inclusion
                           in the morning digest. Returns "" if no tasks.

  get_overdue_alerts()   — fetches overdue Tasks (past due date, not done),
                           returns a list of alert strings for ops.get_alerts().

Both functions use ZohoCRM_searchRecords on the read server (admin-authorized).
The criteria-based search targets the Tasks module by due date and status.

Staff queries ("what tasks are due today?") are answered by the read agent
via ZohoCRM_searchRecords directly — this module is for the scheduled digest
and alert paths only, not for interactive queries.
"""
import json
import logging
from datetime import date, timedelta
from typing import Optional

from zoho_mcp.client import ZohoMCPClient, result_to_text

log = logging.getLogger(__name__)


async def _search_tasks(criteria: str) -> list[dict]:
    """
    Search Zoho CRM Tasks with a criteria string.
    Returns the list of Task records, or empty list on failure.
    Criteria format: (Field_API_Name:operator:value)
    """
    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool("ZohoCRM_searchRecords", {
                "path_variables": {"module": "Tasks"},
                "query_params":   {"criteria": criteria},
            })
        text    = result_to_text(result)
        data    = json.loads(text)
        records = data.get("data", [])
        log.info("[tasks] search '%s' → %d records", criteria[:60], len(records))
        return records
    except Exception as exc:
        log.warning("[tasks] search failed: %s", exc)
        return []


async def get_due_today_text() -> str:
    """
    Fetch Tasks due today from Zoho CRM and format for digest inclusion.
    Returns a WhatsApp-formatted section string, or "" if no tasks due.
    """
    today = date.today().isoformat()    # YYYY-MM-DD
    criteria = f"(Due_Date:equals:{today})AND(Status:not_equal:Completed)"

    tasks = await _search_tasks(criteria)
    if not tasks:
        return ""

    lines = ["", "*📋 Tasks Due Today:*"]
    for t in tasks[:6]:
        subject  = t.get("Subject", "Task")
        assignee = (t.get("Owner", {}) or {}).get("name", "")
        suffix   = f" ({assignee})" if assignee else ""
        lines.append(f"  • {subject}{suffix}")

    return "\n".join(lines)


async def get_overdue_alerts() -> list[str]:
    """
    Fetch overdue Tasks (past due date, not completed) for the alert system.
    Returns a list of alert strings to include in ops.get_alerts().
    """
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    criteria  = f"(Due_Date:before:{yesterday})AND(Status:not_equal:Completed)"

    tasks = await _search_tasks(criteria)
    alerts = []
    for t in tasks[:5]:   # cap at 5 to keep digest readable
        subject  = t.get("Subject", "Unknown task")
        due      = t.get("Due_Date", "unknown date")
        alerts.append(f"⏰ Overdue task: {subject} (was due {due})")

    return alerts