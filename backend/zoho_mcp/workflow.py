# backend/zoho_mcp/workflow.py
"""
Phase 4 — Durable workflow state machine.

A workflow is a named sequence of steps that spans multiple user messages
and multiple Zoho operations. Unlike confirm.py (one action, one message),
a workflow carries context across turns and survives backend restarts.

Currently implemented: Quote-to-Order
────────────────────────────────────
ESTIMATE_CREATED   — estimate exists, waiting for customer to accept
SO_PENDING_APPROVAL — customer confirmed SO, waiting for operator APPROVE
COMPLETED          — SO created and approved, workflow done

Triggered in main.py:
  • After estimate write succeeds    → create workflow (ESTIMATE_CREATED)
  • Customer says "accept / I'll take it" → propose SO, stay in ESTIMATE_CREATED
    (the pending confirm handles the "yes/no"; workflow advances on that "yes")
  • Customer confirms SO (high-risk confirmed) → advance to SO_PENDING_APPROVAL
  • Operator APPROVEs → complete workflow

The workflow store is SQLite so state survives uvicorn restart.
Active workflows expire after WORKFLOW_TTL seconds (default 24h).
"""
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH      = os.getenv("WRITE_ACTIONS_DB_PATH", "./data/write_actions.db")
WORKFLOW_TTL = int(os.getenv("WORKFLOW_TTL", "86400"))   # 24 hours

# ── Step constants ────────────────────────────────────────────────────────────
ESTIMATE_CREATED    = "ESTIMATE_CREATED"
SO_PENDING_APPROVAL = "SO_PENDING_APPROVAL"
COMPLETED           = "COMPLETED"

# ── Message classification keywords ──────────────────────────────────────────
# Used inside workflow context to interpret free-form customer messages.
_ACCEPT_WORDS = {
    "accept", "accepted", "take", "order", "place", "confirm",
    "proceed", "book", "go ahead", "yes please", "sounds good",
    "deal", "agreed", "sure", "okay", "ok",
}
_CANCEL_WORDS  = {"cancel", "no", "stop", "abort", "nevermind", "never mind", "don't"}
_STATUS_WORDS  = {"status", "when", "track", "where", "update", "progress", "shipped"}


@dataclass
class WorkflowState:
    id:            str
    jid:           str
    account_name:  Optional[str]
    wf_type:       str           # "quote_to_order"
    current_step:  str
    context:       dict          # estimate_id, line_items, salesorder_id, …
    created_at:    float
    updated_at:    float
    expires_at:    float
    status:        str           # active | completed | failed | expired


# ── DB setup ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS workflows (
            id            TEXT PRIMARY KEY,
            jid           TEXT NOT NULL,
            account_name  TEXT,
            wf_type       TEXT NOT NULL,
            current_step  TEXT NOT NULL,
            context       TEXT NOT NULL,
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL,
            expires_at    REAL NOT NULL,
            status        TEXT NOT NULL DEFAULT 'active'
        )
    """)
    con.commit()
    con.close()
    log.info("[workflow] table ready in %s", DB_PATH)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_state(row) -> WorkflowState:
    return WorkflowState(
        id           = row["id"],
        jid          = row["jid"],
        account_name = row["account_name"],
        wf_type      = row["wf_type"],
        current_step = row["current_step"],
        context      = json.loads(row["context"]),
        created_at   = row["created_at"],
        updated_at   = row["updated_at"],
        expires_at   = row["expires_at"],
        status       = row["status"],
    )


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create(
    jid:          str,
    account_name: Optional[str],
    wf_type:      str,
    initial_step: str,
    context:      dict,
) -> WorkflowState:
    wf_id = str(uuid.uuid4())[:16]
    now   = time.time()
    exp   = now + WORKFLOW_TTL
    con   = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO workflows "
        "(id,jid,account_name,wf_type,current_step,context,created_at,updated_at,expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (wf_id, jid, account_name, wf_type, initial_step,
         json.dumps(context), now, now, exp),
    )
    con.commit()
    con.close()
    log.info("[workflow] created id=%s type=%s step=%s jid=%s",
             wf_id, wf_type, initial_step, jid)
    return WorkflowState(
        id=wf_id, jid=jid, account_name=account_name,
        wf_type=wf_type, current_step=initial_step,
        context=context, created_at=now, updated_at=now,
        expires_at=exp, status="active",
    )


def get_active(jid: str) -> Optional[WorkflowState]:
    """
    Return the most recent active (non-expired) workflow for this JID, or None.
    Auto-expires stale rows.
    """
    now = time.time()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(
        "UPDATE workflows SET status='expired' "
        "WHERE jid=? AND status='active' AND expires_at < ?",
        (jid, now),
    )
    con.commit()
    row = con.execute(
        "SELECT * FROM workflows WHERE jid=? AND status='active' "
        "ORDER BY created_at DESC LIMIT 1",
        (jid,),
    ).fetchone()
    con.close()
    return _row_to_state(row) if row else None


def get_by_id(wf_id: str) -> Optional[WorkflowState]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM workflows WHERE id=?", (wf_id,)).fetchone()
    con.close()
    return _row_to_state(row) if row else None


def advance(wf_id: str, new_step: str, context_update: dict | None = None) -> bool:
    """Move a workflow to the next step and optionally update its context."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT context FROM workflows WHERE id=?", (wf_id,)).fetchone()
    if not row:
        con.close()
        return False
    ctx = json.loads(row["context"])
    if context_update:
        ctx.update(context_update)
    cur = con.execute(
        "UPDATE workflows SET current_step=?, context=?, updated_at=? WHERE id=?",
        (new_step, json.dumps(ctx), time.time(), wf_id),
    )
    con.commit()
    con.close()
    ok = cur.rowcount > 0
    if ok:
        log.info("[workflow] advanced id=%s → %s", wf_id, new_step)
    return ok


def complete(wf_id: str, context_update: dict | None = None) -> bool:
    """Mark workflow as completed."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT context FROM workflows WHERE id=?", (wf_id,)).fetchone()
    if not row:
        con.close()
        return False
    ctx = json.loads(row["context"])
    if context_update:
        ctx.update(context_update)
    cur = con.execute(
        "UPDATE workflows SET status='completed', current_step=?, context=?, updated_at=? WHERE id=?",
        (COMPLETED, json.dumps(ctx), time.time(), wf_id),
    )
    con.commit()
    con.close()
    ok = cur.rowcount > 0
    if ok:
        log.info("[workflow] completed id=%s", wf_id)
    return ok


def fail(wf_id: str, reason: str) -> bool:
    """
    Mark workflow as failed. Preserves the context so a human can see
    what was completed before the failure (compensation notes).
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT context FROM workflows WHERE id=?", (wf_id,)).fetchone()
    if not row:
        con.close()
        return False
    ctx = json.loads(row["context"])
    ctx["failure_reason"] = reason
    cur = con.execute(
        "UPDATE workflows SET status='failed', context=?, updated_at=? WHERE id=?",
        (json.dumps(ctx), time.time(), wf_id),
    )
    con.commit()
    con.close()
    ok = cur.rowcount > 0
    if ok:
        log.warning("[workflow] failed id=%s reason=%s", wf_id, reason)
    return ok


# ── Message classification within workflow context ────────────────────────────

def classify_in_context(message: str, wf: WorkflowState) -> Optional[str]:
    """
    Interpret a customer message relative to the current workflow state.

    Returns one of:
      "accept_quote"    — customer wants to convert the estimate to an order
      "cancel_workflow" — customer wants to cancel
      "status_check"    — customer asking for progress update
      None              — message doesn't match workflow context;
                          fall through to normal intent classification

    Only called when there is an active workflow AND no pending confirmation.
    """
    normalized = re.sub(r"[^\w\s]", "", message.lower()).strip()
    words      = set(normalized.split())

    if wf.current_step == ESTIMATE_CREATED:
        if words & _ACCEPT_WORDS:
            return "accept_quote"

    # Status check and cancel work at any active step
    if words & _STATUS_WORDS:
        return "status_check"

    if words & _CANCEL_WORDS:
        return "cancel_workflow"

    return None


# ── Workflow factory helpers ───────────────────────────────────────────────────

def start_quote_to_order(
    jid:          str,
    account_name: Optional[str],
    result_text:  str,
    tool_args:    dict,
) -> Optional["WorkflowState"]:
    """
    Called after a ZohoBooks_create_estimate write succeeds.
    Extracts the estimate ID and line items from the Zoho response
    and creates a new Quote-to-Order workflow.
    Returns the new WorkflowState, or None if context can't be extracted.
    """
    try:
        data     = json.loads(result_text)
        estimate = data.get("estimate", {})
        est_id   = estimate.get("estimate_id", "")
        est_num  = estimate.get("estimate_number", "")
    except (json.JSONDecodeError, KeyError):
        log.warning("[workflow] could not parse estimate from result — skipping workflow")
        return None

    if not est_id:
        return None

    # Carry line_items forward so we can pre-fill the SO proposal
    line_items = tool_args.get("body", {}).get("line_items", [])

    ctx = {
        "estimate_id":     est_id,
        "estimate_number": est_num,
        "line_items":      line_items,
    }
    return create(jid, account_name, "quote_to_order", ESTIMATE_CREATED, ctx)