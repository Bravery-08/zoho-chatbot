# backend/zoho_mcp/audit.py
"""
Phase 3 — Append-only action audit log.

Every write action that executes against Zoho is recorded here:
  who (JID + account_name), what (tool + args), when, result,
  and if high-risk, who approved it.

Rows are never updated or deleted — the log is forensic evidence.
The table lives in the same WRITE_ACTIONS_DB_PATH as pending_actions.
"""
import json
import logging
import os
import sqlite3
import time

log = logging.getLogger(__name__)

DB_PATH = os.getenv("WRITE_ACTIONS_DB_PATH", "./data/write_actions.db")


def init_db() -> None:
    """Create the audit table (idempotent — safe to call multiple times)."""
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS action_audit (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp      REAL    NOT NULL,
            jid            TEXT    NOT NULL,
            account_name   TEXT,
            action_id      TEXT    NOT NULL,   -- pending_actions.id
            risk           TEXT    NOT NULL,   -- low | high
            tool_name      TEXT    NOT NULL,
            tool_args      TEXT    NOT NULL,   -- JSON
            result_summary TEXT,               -- first 200 chars of result
            approved_by    TEXT,               -- JID of human approver (high-risk)
            zoho_response  TEXT                -- first 500 chars of raw Zoho JSON
        )
    """)
    con.commit()
    con.close()
    log.info("[audit] table ready in %s", DB_PATH)


def log_action(
    jid:            str,
    account_name:   str | None,
    action_id:      str,
    risk:           str,
    tool_name:      str,
    tool_args:      dict,
    result_summary: str | None  = None,
    zoho_response:  str | None  = None,
    approved_by:    str | None  = None,
) -> None:
    """
    Append one record to the audit log.

    Call this AFTER a write action executes successfully — never before,
    and never for failed or cancelled actions.
    """
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO action_audit "
        "(timestamp, jid, account_name, action_id, risk, tool_name, "
        " tool_args, result_summary, approved_by, zoho_response) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            time.time(),
            jid,
            account_name,
            action_id,
            risk,
            tool_name,
            json.dumps(tool_args),
            (result_summary or "")[:200],
            approved_by,
            (zoho_response or "")[:500],
        ),
    )
    con.commit()
    con.close()
    log.info(
        "[audit] recorded action=%s risk=%s tool=%s account=%s",
        action_id, risk, tool_name, account_name,
    )


def get_customer_history(jid: str, limit: int = 5) -> list[dict]:
    """
    Return the N most recent executed actions for a given customer JID.
    Exposed via /audit/customer endpoint for Phase 5 visibility.
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM action_audit WHERE jid=? ORDER BY timestamp DESC LIMIT ?",
        (jid, limit),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_all_recent(limit: int = 50) -> list[dict]:
    """Return the N most recent audit entries across all customers (staff view)."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM action_audit ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]