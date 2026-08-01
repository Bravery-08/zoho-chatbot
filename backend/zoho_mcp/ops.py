# backend/zoho_mcp/ops.py
"""
Phase 5 — Operational data layer.

Answers questions about the system's own activity from local SQLite
(audit log, workflows, pending actions). No Zoho network call needed.

Used by:
  • The ops_query intent handler in main.py (operator asking questions)
  • digest.py (generating the daily summary text)
  • The alert system (detecting conditions that need attention)
"""
import json
import logging
import os
import sqlite3
import time
from datetime import date, datetime

log = logging.getLogger(__name__)

DB_PATH              = os.getenv("WRITE_ACTIONS_DB_PATH", "./data/write_actions.db")
APPROVAL_ALERT_HOURS = float(os.getenv("APPROVAL_ALERT_HOURS", "4"))
WORKFLOW_STALE_HOURS = float(os.getenv("WORKFLOW_STALE_HOURS", "24"))

# Tool name → human-readable label for digest display
_TOOL_LABELS: dict[str, str] = {
    "ZohoBooks_create_estimate":    "Estimate created",
    "ZohoBooks_create_sales_order": "Sales order created",
    "ZohoCRM_createRecords":        "CRM record created",
}


# ── Core queries ──────────────────────────────────────────────────────────────

def get_daily_summary(for_date: date | None = None) -> dict:
    """
    Return operational counts and recent activity for a given date (default today).

    Result shape:
    {
        "date": "2026-06-26",
        "quotes_created": 3,
        "orders_created": 1,
        "total_actions": 4,
        "active_workflows": 2,
        "pending_approvals": 1,
        "failed_workflows_today": 0,
        "recent_actions": [...],     # last 5 audit entries, formatted
        "workflow_details": [...],   # active workflow rows
        "pending_details": [...],    # awaiting_approval rows
    }
    """
    d         = for_date or date.today()
    day_start = datetime(d.year, d.month, d.day).timestamp()
    day_end   = day_start + 86400

    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row

        # ── Audit counts by tool ──────────────────────────────────────────────
        audit_rows = con.execute(
            "SELECT tool_name, COUNT(*) AS cnt FROM action_audit "
            "WHERE timestamp >= ? AND timestamp < ? GROUP BY tool_name",
            (day_start, day_end),
        ).fetchall()

        quotes_created = sum(r["cnt"] for r in audit_rows if "estimate"   in r["tool_name"])
        orders_created = sum(r["cnt"] for r in audit_rows if "sales_order" in r["tool_name"])
        total_actions  = sum(r["cnt"] for r in audit_rows)

        # ── Active workflows ──────────────────────────────────────────────────
        active_wfs = con.execute(
            "SELECT id, account_name, wf_type, current_step, context, created_at "
            "FROM workflows WHERE status='active' ORDER BY created_at DESC"
        ).fetchall()

        # ── Pending approvals ─────────────────────────────────────────────────
        pending = con.execute(
            "SELECT id, jid, account_name, proposal_text, created_at "
            "FROM pending_actions WHERE status='awaiting_approval'"
        ).fetchall()

        # ── Failed workflows today ────────────────────────────────────────────
        failed_count = con.execute(
            "SELECT COUNT(*) AS c FROM workflows "
            "WHERE status='failed' AND updated_at >= ?",
            (day_start,),
        ).fetchone()["c"]

        # ── Recent audit entries ──────────────────────────────────────────────
        recent = con.execute(
            "SELECT timestamp, account_name, tool_name, risk, result_summary "
            "FROM action_audit ORDER BY timestamp DESC LIMIT 5"
        ).fetchall()

        con.close()

        return {
            "date":                   d.isoformat(),
            "quotes_created":         quotes_created,
            "orders_created":         orders_created,
            "total_actions":          total_actions,
            "active_workflows":       len(active_wfs),
            "pending_approvals":      len(pending),
            "failed_workflows_today": failed_count,
            "recent_actions": [
                {
                    "time":     datetime.fromtimestamp(r["timestamp"]).strftime("%H:%M"),
                    "account":  r["account_name"] or "Unknown",
                    "action":   _TOOL_LABELS.get(r["tool_name"], r["tool_name"]),
                    "risk":     r["risk"],
                }
                for r in recent
            ],
            "workflow_details": [
                {
                    "id":           r["id"],
                    "account":      r["account_name"],
                    "step":         r["current_step"],
                    "estimate":     json.loads(r["context"]).get("estimate_number", ""),
                    "created_ago_h": round((time.time() - r["created_at"]) / 3600, 1),
                }
                for r in active_wfs
            ],
            "pending_details": [
                {
                    "id":         r["id"],
                    "account":    r["account_name"],
                    "proposal":   r["proposal_text"][:80],
                    "pending_h":  round((time.time() - r["created_at"]) / 3600, 1),
                }
                for r in pending
            ],
        }

    except Exception as exc:
        log.error("[ops] get_daily_summary failed: %s", exc)
        return {
            "date": d.isoformat(),
            "quotes_created": 0, "orders_created": 0,
            "total_actions": 0, "active_workflows": 0,
            "pending_approvals": 0, "failed_workflows_today": 0,
            "recent_actions": [], "workflow_details": [], "pending_details": [],
            "error": str(exc),
        }


def get_alerts() -> list[str]:
    """
    Return alert strings for conditions needing operator attention:
      - Approvals pending longer than APPROVAL_ALERT_HOURS
      - Estimates not accepted for longer than WORKFLOW_STALE_HOURS
      - Workflow failures in the last 24 hours
    """
    alerts: list[str] = []
    now             = time.time()
    approval_cutoff = now - APPROVAL_ALERT_HOURS * 3600
    stale_cutoff    = now - WORKFLOW_STALE_HOURS * 3600
    day_ago         = now - 86400

    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row

        # Approvals stuck too long
        for row in con.execute(
            "SELECT id, account_name, proposal_text, created_at FROM pending_actions "
            "WHERE status='awaiting_approval' AND created_at < ?",
            (approval_cutoff,),
        ).fetchall():
            h = (now - row["created_at"]) / 3600
            alerts.append(
                f"⏰ Approval pending {h:.1f}h: "
                f"{(row['account_name'] or 'Unknown')} — "
                f"{row['proposal_text'][:50]}"
            )

        # Estimates not accepted
        for row in con.execute(
            "SELECT id, account_name, context, created_at FROM workflows "
            "WHERE status='active' AND current_step='ESTIMATE_CREATED' AND created_at < ?",
            (stale_cutoff,),
        ).fetchall():
            h   = (now - row["created_at"]) / 3600
            ctx = json.loads(row["context"])
            alerts.append(
                f"📋 Quote unaccepted {h:.0f}h: "
                f"{ctx.get('estimate_number', '?')} "
                f"({row['account_name'] or 'Unknown'})"
            )

        # Workflow failures today
        for row in con.execute(
            "SELECT account_name, context FROM workflows "
            "WHERE status='failed' AND updated_at > ?",
            (day_ago,),
        ).fetchall():
            ctx = json.loads(row["context"])
            alerts.append(
                f"❌ Workflow failed: {ctx.get('failure_reason', 'unknown reason')} "
                f"({row['account_name'] or 'Unknown'})"
            )

        con.close()
    except Exception as exc:
        log.error("[ops] get_alerts failed: %s", exc)
        alerts.append(f"⚠️ Alert check failed: {exc}")

    return alerts

async def get_task_alerts() -> list[str]:
    """
    Fetch overdue CRM Tasks to include in alerts.
    Async because it queries Zoho CRM via the read server.
    Called from the digest scheduler in main.py (async context).
    """
    try:
        from zoho_mcp.tasks import get_overdue_alerts
        return await get_overdue_alerts()
    except Exception as exc:
        log.error("[ops] task alerts failed: %s", exc)
        return []