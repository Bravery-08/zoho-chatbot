# backend/zoho_mcp/digest.py
"""
Phase 5 — Daily digest generation and outbox.

Digest: a formatted WhatsApp message summarising the day's operations,
active workflows, pending approvals, and alerts. Generated daily by the
background scheduler in main.py and pushed to every staff JID.

Outbox: a lightweight SQLite table (in write_actions.db) that queues
outbound messages for the Baileys bot to deliver. The bot polls
GET /outbox/pending and marks items delivered via POST /outbox/{id}/delivered.

This decouples the Python backend from the Baileys send API — the bot
can restart or reconnect without losing queued messages.
"""
import logging
import os
import sqlite3
import time
import uuid
from datetime import date

from zoho_mcp.ops import get_daily_summary, get_alerts

log = logging.getLogger(__name__)

DB_PATH = os.getenv("WRITE_ACTIONS_DB_PATH", "./data/write_actions.db")


# ── Outbox DB setup ───────────────────────────────────────────────────────────

def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS outbox (
            id           TEXT PRIMARY KEY,
            jid          TEXT NOT NULL,       -- recipient WhatsApp JID
            message      TEXT NOT NULL,
            created_at   REAL NOT NULL,
            delivered_at REAL,
            status       TEXT NOT NULL DEFAULT 'pending'
            -- pending | delivered | failed
        )
    """)
    con.commit()
    con.close()
    log.info("[digest] outbox table ready in %s", DB_PATH)


# ── Digest generation ─────────────────────────────────────────────────────────

def generate_digest_text(for_date: date | None = None) -> str:
    """
    Generate a WhatsApp-formatted daily digest string.

    Format uses WhatsApp bold (*text*) for section headers.
    Kept to ~20 lines so it's readable without scrolling on mobile.
    """
    summary = get_daily_summary(for_date)
    alerts  = get_alerts()
    d       = (for_date or date.today()).strftime("%d %b %Y")

    lines = [
        f"📊 *Daily Operations — {d}*",
        "",
        f"Quotes Created    : {summary['quotes_created']}",
        f"Sales Orders      : {summary['orders_created']}",
        f"Active Workflows  : {summary['active_workflows']}",
        f"Pending Approvals : {summary['pending_approvals']}",
    ]

    if summary["failed_workflows_today"]:
        lines.append(f"⚠️ Failed Workflows : {summary['failed_workflows_today']}")

    # Recent activity
    if summary["recent_actions"]:
        lines += ["", "*Recent Activity:*"]
        for r in summary["recent_actions"][:4]:
            lines.append(f"  • {r['time']} — {r['account']} — {r['action']}")

    # Pending approvals detail
    if summary["pending_details"]:
        lines += ["", "*Awaiting Approval:*"]
        for p in summary["pending_details"]:
            lines.append(
                f"  • {p['account']} — {p['proposal'][:50]} "
                f"({p['pending_h']:.1f}h)"
            )

    # Alerts
    if alerts:
        lines += ["", "*⚠️ Alerts:*"]
        for a in alerts:
            lines.append(f"  {a}")
    else:
        lines += ["", "✅ No alerts"]

    return "\n".join(lines)


# ── Outbox operations ─────────────────────────────────────────────────────────

def schedule_to_outbox(jid: str, message: str) -> str:
    """
    Queue a message for delivery. Returns the outbox message ID.
    The Baileys bot polls GET /outbox/pending and delivers queued messages.
    """
    mid = str(uuid.uuid4())[:16]
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO outbox (id, jid, message, created_at) VALUES (?,?,?,?)",
        (mid, jid, message, time.time()),
    )
    con.commit()
    con.close()
    log.info("[digest] queued message mid=%s for %s", mid, jid)
    return mid


def get_pending_outbox() -> list[dict]:
    """Return up to 20 pending outbox messages (oldest first)."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, jid, message, created_at FROM outbox "
        "WHERE status='pending' ORDER BY created_at ASC LIMIT 20"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def mark_delivered(message_id: str) -> None:
    """Called by the Baileys bot after successfully delivering a message."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE outbox SET status='delivered', delivered_at=? WHERE id=?",
        (time.time(), message_id),
    )
    con.commit()
    con.close()
    log.info("[digest] message mid=%s marked delivered", message_id)


def mark_failed(message_id: str) -> None:
    """Called by the Baileys bot if delivery fails."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE outbox SET status='failed' WHERE id=?", (message_id,)
    )
    con.commit()
    con.close()
    log.warning("[digest] message mid=%s marked failed", message_id)