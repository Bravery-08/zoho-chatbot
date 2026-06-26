# backend/zoho_mcp/confirm.py
"""
Phase 3 — Pending action store (confirm-then-execute state machine).

Every write action goes through a two-turn confirmation:
  Turn 1: agent proposes → PendingAction created with status='pending'
  Turn 2: customer replies → 'confirmed' | 'cancelled' | None (unclear)

Idempotency: each action has a unique ID (hash of JID + tool + timestamp).
If the customer taps "yes" twice, the second call finds status='executed'
and returns None so the write is not repeated.

High-risk actions follow the same flow but after confirmation they move to
status='awaiting_approval' and wait for a human APPROVE command instead
of executing immediately.

Tables live in WRITE_ACTIONS_DB_PATH (separate from escalations.db).
"""
import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH            = os.getenv("WRITE_ACTIONS_DB_PATH", "./data/write_actions.db")
CONFIRMATION_TTL   = int(os.getenv("CONFIRMATION_TIMEOUT", "600"))  # seconds

# ── Confirmation vocabulary ───────────────────────────────────────────────────
_YES = {"yes", "y", "confirm", "confirmed", "ok", "okay", "sure", "proceed",
        "go ahead", "do it", "yep", "yeah", "approved", "agree"}
_NO  = {"no", "n", "cancel", "cancelled", "stop", "abort", "nope", "nah",
        "never", "nevermind", "dont", "reject"}


@dataclass
class PendingAction:
    id:            str
    jid:           str
    account_name:  Optional[str]
    created_at:    float
    expires_at:    float
    risk:          str          # "low" | "high"
    tool_name:     str
    tool_args:     dict
    proposal_text: str
    status:        str          # pending | confirmed | cancelled | executed
                                # | awaiting_approval | approved | rejected | expired


# ── DB setup ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pending_actions (
            id            TEXT PRIMARY KEY,
            jid           TEXT NOT NULL,
            account_name  TEXT,
            created_at    REAL NOT NULL,
            expires_at    REAL NOT NULL,
            risk          TEXT NOT NULL,
            tool_name     TEXT NOT NULL,
            tool_args     TEXT NOT NULL,
            proposal_text TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending'
        )
    """)
    con.commit()
    con.close()
    log.info("[confirm] DB ready at %s", DB_PATH)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_action_id(jid: str, tool_name: str) -> str:
    """12-char hex idempotency key — unique per JID + tool + moment."""
    raw = f"{jid}:{tool_name}:{time.time()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def parse_response(message: str) -> Optional[str]:
    """
    Return 'confirmed', 'cancelled', or None if the message is ambiguous.
    Strips punctuation before matching so 'Yeah,' matches 'yeah'.
    """
    import re as _re
    normalized = _re.sub(r"[^\w\s]", "", message.lower()).strip()
    words = set(normalized.split())
    if words & _YES:
        return "confirmed"
    if words & _NO:
        return "cancelled"
    return None


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create_pending(
    jid:           str,
    account_name:  Optional[str],
    risk:          str,
    tool_name:     str,
    tool_args:     dict,
    proposal_text: str,
) -> PendingAction:
    """Store a new pending action and return it."""
    action_id  = make_action_id(jid, tool_name)
    now        = time.time()
    expires_at = now + CONFIRMATION_TTL
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO pending_actions "
        "(id, jid, account_name, created_at, expires_at, risk, tool_name, tool_args, proposal_text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (action_id, jid, account_name, now, expires_at,
         risk, tool_name, json.dumps(tool_args), proposal_text),
    )
    con.commit()
    con.close()
    log.info("[confirm] created action=%s risk=%s tool=%s", action_id, risk, tool_name)
    return PendingAction(
        id=action_id, jid=jid, account_name=account_name,
        created_at=now, expires_at=expires_at, risk=risk,
        tool_name=tool_name, tool_args=tool_args,
        proposal_text=proposal_text, status="pending",
    )


def get_pending(jid: str) -> Optional[PendingAction]:
    """
    Return the most recent non-expired, non-resolved action for this JID,
    or None if there is no such action.
    Automatically marks expired rows so they don't accumulate.
    """
    now = time.time()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Expire stale pending rows first
    con.execute(
        "UPDATE pending_actions SET status='expired' "
        "WHERE jid=? AND status='pending' AND expires_at < ?",
        (jid, now),
    )
    con.commit()

    row = con.execute(
        "SELECT * FROM pending_actions "
        "WHERE jid=? AND status IN ('pending','awaiting_approval') "
        "ORDER BY created_at DESC LIMIT 1",
        (jid,),
    ).fetchone()
    con.close()

    if not row:
        return None
    return PendingAction(
        id=row["id"], jid=row["jid"], account_name=row["account_name"],
        created_at=row["created_at"], expires_at=row["expires_at"],
        risk=row["risk"], tool_name=row["tool_name"],
        tool_args=json.loads(row["tool_args"]),
        proposal_text=row["proposal_text"], status=row["status"],
    )


def get_by_id(action_id: str) -> Optional[PendingAction]:
    """Fetch a specific action by ID (used by the approval flow)."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM pending_actions WHERE id=?", (action_id,)
    ).fetchone()
    con.close()
    if not row:
        return None
    return PendingAction(
        id=row["id"], jid=row["jid"], account_name=row["account_name"],
        created_at=row["created_at"], expires_at=row["expires_at"],
        risk=row["risk"], tool_name=row["tool_name"],
        tool_args=json.loads(row["tool_args"]),
        proposal_text=row["proposal_text"], status=row["status"],
    )


def update_status(action_id: str, status: str) -> bool:
    """Update action status. Returns True if a row was updated."""
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "UPDATE pending_actions SET status=? WHERE id=?", (status, action_id)
    )
    con.commit()
    con.close()
    return cur.rowcount > 0