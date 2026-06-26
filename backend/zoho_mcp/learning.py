# backend/zoho_mcp/learning.py
"""
Phase 6 — Learning loop and graduated autonomy.

Two mechanisms that make the system more autonomous over time:

1. FEEDBACK STORE
   Every operator APPROVE / REJECT / modify on a high-risk action is
   recorded alongside the tool name and customer account. This creates
   an auditable trail of human decisions and surfaces patterns (e.g. the
   operator always reduces the quantity by 10%).

2. GRADUATED AUTONOMY
   High-risk tools (ZohoBooks_create_sales_order, …) start requiring human
   approval for every action. As a tool-account pair accumulates consecutive
   clean approvals (no modifications, no rejections), it is promoted to
   low_risk and will auto-execute after the customer confirms — skipping the
   human approval queue entirely.

   Promotion threshold : GRADUATION_THRESHOLD consecutive clean approvals
   Demotion threshold  : DEGRADE_THRESHOLD consecutive rejections (post-graduation)

   Approval with modification = not clean (streak resets to 0)
   Rejection                  = not clean (streak resets to 0)
   Execution failure          = does not affect the trust level
                                (tracked separately via audit log)

REGRESSION SUITE
   Production failures (execute_with_retry returns None) are formatted as
   corpus.jsonl lines via format_corpus_entry(). Add the logged entry to
   backend/zoho_mcp/corpus.jsonl and re-run python -m zoho_mcp.run_eval to
   confirm the regression is caught by the eval suite.

   Gate: ≥95% tool-selection accuracy must be maintained before merging
   any prompt or model change. Run the eval first; block the change if it
   drops below the gate.
"""
import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH              = os.getenv("WRITE_ACTIONS_DB_PATH", "./data/write_actions.db")
GRADUATION_THRESHOLD = int(os.getenv("GRADUATION_THRESHOLD", "5"))
DEGRADE_THRESHOLD    = int(os.getenv("DEGRADE_THRESHOLD",   "2"))


# ── DB setup ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    con = sqlite3.connect(DB_PATH)

    con.execute("""
        CREATE TABLE IF NOT EXISTS action_feedback (
            id            TEXT PRIMARY KEY,
            action_id     TEXT NOT NULL,       -- pending_actions.id
            timestamp     REAL NOT NULL,
            tool_name     TEXT NOT NULL,
            account_name  TEXT,
            decision      TEXT NOT NULL,       -- approved | rejected | modified
            modification  TEXT,                -- operator's note / changed values
            approved_by   TEXT                 -- operator JID
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS trust_levels (
            id                TEXT PRIMARY KEY,   -- "{tool}::{account|*}"
            tool_name         TEXT NOT NULL,
            account_name      TEXT,               -- NULL = wildcard (all accounts)
            clean_streak      INTEGER DEFAULT 0,  -- consecutive clean approvals
            total_approvals   INTEGER DEFAULT 0,
            total_rejections  INTEGER DEFAULT 0,
            consec_rejections INTEGER DEFAULT 0,  -- used for demotion check
            risk_level        TEXT NOT NULL DEFAULT 'high',  -- high | low
            graduated_at      REAL,               -- when first promoted
            last_updated      REAL NOT NULL
        )
    """)

    con.commit()
    con.close()
    log.info("[learning] tables ready in %s", DB_PATH)


# ── Trust key ─────────────────────────────────────────────────────────────────

def _trust_key(tool_name: str, account_name: Optional[str]) -> str:
    """Unique ID for a tool-account combination. '*' means all accounts."""
    return f"{tool_name}::{account_name or '*'}"


# ── Read trust level ──────────────────────────────────────────────────────────

def get_effective_risk(tool_name: str, account_name: Optional[str]) -> str:
    """
    Return the effective risk level for a tool-account pair.

    Checks the specific account first, then the wildcard ('*') entry.
    Returns 'low' if the pair has graduated, otherwise 'high'.
    Falls back to 'high' on any DB error.
    """
    keys = [_trust_key(tool_name, account_name)]
    if account_name:
        keys.append(_trust_key(tool_name, None))   # wildcard fallback

    try:
        con = sqlite3.connect(DB_PATH)
        for key in keys:
            row = con.execute(
                "SELECT risk_level FROM trust_levels WHERE id=?", (key,)
            ).fetchone()
            if row and row[0] == "low":
                con.close()
                log.info("[learning] %s → graduated (low risk)", key)
                return "low"
        con.close()
    except Exception as exc:
        log.error("[learning] get_effective_risk failed: %s", exc)

    return "high"


# ── Record feedback ───────────────────────────────────────────────────────────

def record_feedback(
    action_id:    str,
    tool_name:    str,
    account_name: Optional[str],
    decision:     str,               # "approved" | "rejected" | "modified"
    modification: Optional[str] = None,
    approved_by:  Optional[str] = None,
) -> None:
    """
    Record operator feedback on a high-risk action and update the trust streak.

    Clean approval  (decision=approved, no modification)  → streak +1
    Modified        (decision=modified)                   → streak reset to 0
    Rejection       (decision=rejected)                   → streak reset to 0,
                                                            consec_rejections +1
                                                            (may trigger demotion)

    Promotion: clean_streak reaches GRADUATION_THRESHOLD → risk_level = 'low'
    Demotion:  consec_rejections reaches DEGRADE_THRESHOLD (post-graduation)
               → risk_level = 'high'
    """
    now = time.time()
    key = _trust_key(tool_name, account_name)

    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row

        # Store the raw feedback event
        con.execute(
            "INSERT INTO action_feedback "
            "(id, action_id, timestamp, tool_name, account_name, decision, modification, approved_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4())[:16], action_id, now,
             tool_name, account_name, decision, modification, approved_by),
        )

        # Update trust level
        row = con.execute(
            "SELECT clean_streak, total_approvals, total_rejections, "
            "consec_rejections, risk_level "
            "FROM trust_levels WHERE id=?", (key,)
        ).fetchone()

        if row:
            streak           = row["clean_streak"]
            tot_approvals    = row["total_approvals"]
            tot_rejections   = row["total_rejections"]
            consec_rejections = row["consec_rejections"]
            current_risk     = row["risk_level"]
        else:
            streak = tot_approvals = tot_rejections = consec_rejections = 0
            current_risk = "high"

        if decision == "approved":
            new_streak           = streak + 1
            new_tot_approvals    = tot_approvals + 1
            new_tot_rejections   = tot_rejections
            new_consec_rejections = 0             # clean approval resets rejection run
        elif decision == "rejected":
            new_streak           = 0
            new_tot_approvals    = tot_approvals
            new_tot_rejections   = tot_rejections + 1
            new_consec_rejections = consec_rejections + 1
        else:   # modified
            new_streak           = 0
            new_tot_approvals    = tot_approvals
            new_tot_rejections   = tot_rejections
            new_consec_rejections = 0

        # Graduation check
        graduated_at_update = None
        if new_streak >= GRADUATION_THRESHOLD and current_risk == "high":
            new_risk         = "low"
            graduated_at_update = now
            log.info(
                "[learning] 🎓 GRADUATED: %s → low risk (streak=%d)",
                key, new_streak,
            )
        # Demotion check (only if currently graduated)
        elif new_consec_rejections >= DEGRADE_THRESHOLD and current_risk == "low":
            new_risk         = "high"
            graduated_at_update = None   # clear graduation timestamp
            log.warning(
                "[learning] 📉 DEMOTED: %s → high risk (consec_rejections=%d)",
                key, new_consec_rejections,
            )
        else:
            new_risk = current_risk

        if row:
            con.execute(
                "UPDATE trust_levels SET "
                "clean_streak=?, total_approvals=?, total_rejections=?, "
                "consec_rejections=?, risk_level=?, last_updated=? "
                "WHERE id=?",
                (new_streak, new_tot_approvals, new_tot_rejections,
                 new_consec_rejections, new_risk, now, key),
            )
            if graduated_at_update is not None:
                con.execute(
                    "UPDATE trust_levels SET graduated_at=? WHERE id=?",
                    (graduated_at_update, key),
                )
            elif new_risk == "high" and current_risk == "low":
                # Clear graduation date on demotion
                con.execute(
                    "UPDATE trust_levels SET graduated_at=NULL WHERE id=?", (key,)
                )
        else:
            con.execute(
                "INSERT INTO trust_levels "
                "(id, tool_name, account_name, clean_streak, total_approvals, "
                "total_rejections, consec_rejections, risk_level, last_updated) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (key, tool_name, account_name, new_streak, new_tot_approvals,
                 new_tot_rejections, new_consec_rejections, new_risk, now),
            )

        con.commit()
        con.close()

        log.info(
            "[learning] feedback: tool=%s account=%s decision=%s "
            "streak=%d/%d risk=%s",
            tool_name, account_name or "*", decision,
            new_streak, GRADUATION_THRESHOLD, new_risk,
        )

    except Exception as exc:
        log.error("[learning] record_feedback failed: %s", exc)


# ── Visibility ────────────────────────────────────────────────────────────────

def get_graduation_status() -> list[dict]:
    """
    Return all trust level records for visibility in /ops/summary.
    Shows which tools are graduated, how close others are, and the
    streak progress toward graduation.
    """
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT *, "
            "CAST(clean_streak AS TEXT) || '/' || ? || ' to graduation' AS progress "
            "FROM trust_levels ORDER BY tool_name, account_name",
            (GRADUATION_THRESHOLD,),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.error("[learning] get_graduation_status failed: %s", exc)
        return []


def get_recent_feedback(limit: int = 10) -> list[dict]:
    """Return the N most recent operator feedback events."""
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM action_feedback ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.error("[learning] get_recent_feedback failed: %s", exc)
        return []


# ── Corpus helper ─────────────────────────────────────────────────────────────

def format_corpus_entry(
    message:       str,
    expected_tool: Optional[str],
    actual_result: str,
    category:      str = "production_failure",
) -> str:
    """
    Format a production failure as a corpus.jsonl line.

    USAGE:
    When a tool call fails in production, the caller logs:
        log.warning("[learning] ADD TO CORPUS: %s", learning.format_corpus_entry(...))

    Grep logs for "ADD TO CORPUS", review each entry, and add valid ones to
    backend/zoho_mcp/corpus.jsonl. Re-run:
        python -m zoho_mcp.run_eval
    The eval must still pass ≥95% — if it doesn't, the failure exposes a
    real regression in tool selection that must be fixed before any merge.

    REGRESSION GATE:
    Run the eval before AND after any change to:
      • The agent routing prompt (_ROUTING_PROMPT in agent.py)
      • The intent classifier system prompt (intent.py)
      • The model name (AGENT_MODEL or INTENT_MODEL)
    If accuracy drops below 95%, do not merge. Fix the prompt first.
    """
    row_id = f"P{int(time.time()) % 100000:05d}"
    entry = {
        "id":            row_id,
        "command":       message,
        "expected_tool": expected_tool,
        "expected_args": {},
        "category":      category,
        "_production_note": actual_result[:120],
    }
    return json.dumps(entry)