"""
backend/test_suite.py
─────────────────────
Complete integration test suite for the WhatsApp × Zoho One system.
Runs against a live FastAPI backend. No WhatsApp, no Baileys needed.

SETUP
─────
1. Start the backend first:
       uvicorn app.main:app --reload --port 8000

2. Edit the CONFIG section below — set STAFF_JID and CUSTOMER_JID
   to match your actual JIDs (check uvicorn logs for the exact format).

3. Run:
       python test_suite.py                  # all tests
       python test_suite.py --fast           # skip Zoho network tests
       python test_suite.py --section write  # one section only

NOTES
─────
- Tests marked [ZOHO] create real records in Zoho Books.
  They appear as draft estimates / sales orders — safe to delete after.
- Tests run sequentially. Some tests depend on state from earlier ones
  (e.g. the workflow test chains 4 requests in order).
- Default timeout per request: 30s. Zoho MCP calls take 2–5s each.
"""

import sys, os
# Ensure backend/ is on the path so zoho_mcp can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import os
import re
import sqlite3
import sys
import time
import argparse
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("requests not installed — run: pip install requests")

# ═════════════════════════════════════════════════════════════════════════════
# CONFIG — edit these to match your environment
# ═════════════════════════════════════════════════════════════════════════════

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8000")
DB_PATH  = os.getenv("WRITE_ACTIONS_DB_PATH", "./data/write_actions.db")

# The JID that appears in uvicorn logs for your personal number.
# Check the log: "Query from 141407654273204@lid" or "919876543210@s.whatsapp.net"
STAFF_JID    = os.getenv("TEST_STAFF_JID",    "141407654273204@lid")

# A JID that maps to a real CRM Contact (your test customer)
CUSTOMER_JID = os.getenv("TEST_CUSTOMER_JID", "919876543210@s.whatsapp.net")

# A JID that definitely isn't in CRM
UNKNOWN_JID  = os.getenv("TEST_UNKNOWN_JID",  "919999999999@s.whatsapp.net")

# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

results = {"passed": 0, "failed": 0, "skipped": 0}
failures = []


def q(message: str, sender: str, history: list | None = None, timeout: int = 30) -> dict:
    """POST /query with automatic 429 retry."""
    time.sleep(2)   # base delay between every call
    for attempt in range(4):
        r = requests.post(
            f"{BASE_URL}/query",
            json={"message": message, "sender": sender, "history": history or []},
            timeout=timeout,
        )
        if r.status_code == 429:
            wait = 20 * (attempt + 1)   # 20s, 40s, 60s
            print(f"\n  {YELLOW}⏳ Rate limited — waiting {wait}s...{RESET}")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()   # raise on 4th failure
    return r.json()


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        results["passed"] += 1
        print(f"  {GREEN}✓{RESET} {name}")
    else:
        results["failed"] += 1
        detail_str = f" {DIM}— {detail}{RESET}" if detail else ""
        print(f"  {RED}✗{RESET} {name}{detail_str}")
        failures.append(f"{current_section} › {name}" + (f" ({detail})" if detail else ""))


def skip(name: str, reason: str = "") -> None:
    results["skipped"] += 1
    reason_str = f" {DIM}({reason}){RESET}" if reason else ""
    print(f"  {YELLOW}○{RESET} {name}{reason_str}")


def section(title: str) -> None:
    global current_section
    current_section = title
    print(f"\n{BOLD}{BLUE}{'─' * 62}{RESET}")
    print(f"{BOLD}{BLUE}  {title}{RESET}")
    print(f"{BOLD}{BLUE}{'─' * 62}{RESET}")


current_section = ""


def db_get_latest_pending(jid: str, status: str = "pending") -> Optional[dict]:
    """Fetch the most recent pending_actions row for this JID."""
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM pending_actions WHERE jid=? AND status=? "
            "ORDER BY created_at DESC LIMIT 1",
            (jid, status),
        ).fetchone()
        con.close()
        return dict(row) if row else None
    except Exception:
        return None


def db_get_latest_workflow(jid: str) -> Optional[dict]:
    """Fetch the most recent active workflow for this JID."""
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM workflows WHERE jid=? AND status='active' "
            "ORDER BY created_at DESC LIMIT 1",
            (jid,),
        ).fetchone()
        con.close()
        return dict(row) if row else None
    except Exception:
        return None


def db_get_latest_audit(jid: str) -> Optional[dict]:
    """Fetch the most recent audit record for this JID."""
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM action_audit WHERE jid=? ORDER BY timestamp DESC LIMIT 1",
            (jid,),
        ).fetchone()
        con.close()
        return dict(row) if row else None
    except Exception:
        return None


def approve(action_id: str, note: str = "") -> dict:
    answer = f"APPROVE {action_id}" + (f" {note}" if note else "")
    r = requests.post(
        f"{BASE_URL}/escalate/resolve",
        json={"answer": answer, "notification_msg_id": ""},
        timeout=30,
    )
    return r.json()


def reject(action_id: str) -> dict:
    r = requests.post(
        f"{BASE_URL}/escalate/resolve",
        json={"answer": f"REJECT {action_id}", "notification_msg_id": ""},
        timeout=30,
    )
    return r.json()


def is_zoho_available() -> bool:
    """Quick check — can we hit the health endpoint? (Zoho availability checked separately)"""
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 0 — INFRASTRUCTURE
# ═════════════════════════════════════════════════════════════════════════════

def test_infrastructure():
    section("0 — Infrastructure")

    # Health endpoint
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=5)
        check("Backend is reachable", r.status_code == 200)
    except Exception as exc:
        check("Backend is reachable", False, str(exc))
        print(f"\n  {RED}Cannot reach {BASE_URL} — is uvicorn running?{RESET}")
        sys.exit(1)

    # SQLite DB exists and has all required tables
    try:
        con = sqlite3.connect(DB_PATH)
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        con.close()
        required = {"pending_actions","action_audit","workflows","outbox",
                    "action_feedback","trust_levels"}
        missing  = required - tables
        check("All SQLite tables exist",
              len(missing) == 0,
              f"missing: {missing}" if missing else "")
    except Exception as exc:
        check("SQLite DB accessible", False, str(exc))

    # Ops summary returns expected structure
    r = requests.get(f"{BASE_URL}/ops/summary", timeout=10)
    check("GET /ops/summary returns 200", r.status_code == 200)
    body = r.json()
    check("ops/summary has required keys",
          all(k in body for k in ("summary","alerts","graduation","feedback")))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — INTENT ROUTING
# ═════════════════════════════════════════════════════════════════════════════

def test_intent_routing():
    section("1 — Intent Routing")
    # All tests use STAFF_JID to avoid identity-related routing interference

    cases = [
        ("How many quotes did we create today?", "ops",
         "ops_query → ops route"),
        ("What is your minimum order quantity?", "rag",
         "answer_from_kb → rag route (if sops.txt has MOQ)"),
        ("What is 15% of 50000?",               "general",
         "general → general route"),
        ("I want to speak to a manager",         "escalate",
         "escalate → escalate route"),
    ]

    for message, expected_route, name in cases:
        try:
            resp = q(message, STAFF_JID)
            actual = resp.get("route", "")
            check(name, actual == expected_route,
                  f"got route='{actual}', expected='{expected_route}'")
        except Exception as exc:
            check(name, False, str(exc))

    # write_zoho intent produces write_proposal route
    try:
        resp = q("I need a quote for 50 bags of basmati rice at 2800 per bag",
                 CUSTOMER_JID)
        route = resp.get("route", "")
        check("write_zoho → write_proposal route",
              route == "write_proposal",
              f"got '{route}'")
        # Cancel it immediately to leave no dangling state
        pending = db_get_latest_pending(STAFF_JID)
        if pending:
            q("no", STAFF_JID)
    except Exception as exc:
        check("write_zoho → write_proposal route", False, str(exc))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — IDENTITY AND TRUST BOUNDARY
# ═════════════════════════════════════════════════════════════════════════════

def test_identity():
    section("2 — Identity and Trust Boundary")

    # Internal (staff) identity
    resp = q("How many quotes today?", STAFF_JID)
    check("Staff JID routes to ops (not escalate)",
          resp.get("route") == "ops",
          f"got '{resp.get('route')}'")

    # Unknown identity blocks Zoho reads
    resp = q("Show my invoices", UNKNOWN_JID)
    check("Unknown JID gets 'not in system' message",
          "not in system" in resp.get("response","").lower() or
          resp.get("route") == "unknown",
          f"route={resp.get('route')} response={resp.get('response','')[:60]}")

    # Unknown identity still gets KB/general answers
    resp = q("What is 2 plus 2?", UNKNOWN_JID)
    check("Unknown JID can still ask general questions",
          resp.get("route") in ("general","rag"),
          f"got '{resp.get('route')}'")

    # Ops query blocked for non-internal users
    resp = q("How many quotes today?", UNKNOWN_JID)
    check("ops_query blocked for unknown JID",
          resp.get("route") in ("escalate","unknown"),
          f"got '{resp.get('route')}'")

    # Known customer — ops query should also be blocked
    if CUSTOMER_JID != UNKNOWN_JID:
        resp = q("How many quotes today?", CUSTOMER_JID)
        check("ops_query blocked for known customer",
              resp.get("route") in ("escalate","unknown"),
              f"got '{resp.get('route')}'")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — ZOHO READS
# ═════════════════════════════════════════════════════════════════════════════

def test_zoho_reads(fast: bool = False):
    section("3 — Zoho Reads [ZOHO]")

    if fast:
        skip("All Zoho read tests", "--fast flag set")
        return

    cases = [
        ("Show all unpaid invoices",    STAFF_JID,    "zoho"),
        ("List all customers",          STAFF_JID,    "zoho"),
        ("Show all sales orders",       STAFF_JID,    "zoho"),
        ("What are all our estimates?", STAFF_JID,    "zoho"),
    ]

    for message, sender, expected_route in cases:
        try:
            resp = q(message, sender)
            check(f"'{message[:40]}' → {expected_route}",
                  resp.get("route") == expected_route,
                  f"got '{resp.get('route')}' — {resp.get('response','')[:80]}")
        except Exception as exc:
            check(f"'{message[:40]}'", False, str(exc))

    # Scoping: customer asking for another account's invoices gets their own
    if CUSTOMER_JID != UNKNOWN_JID:
        try:
            resp = q("Show invoices for Acme Exports", CUSTOMER_JID)
            route = resp.get("route","")
            response_text = resp.get("response","").lower()
            # Should NOT see Acme's data — should see their own or "no invoices"
            con = sqlite3.connect(DB_PATH)
            audit_row = con.execute(
                "SELECT tool_args FROM action_audit WHERE jid=? ORDER BY timestamp DESC LIMIT 1",
                (CUSTOMER_JID,)
            ).fetchone()
            con.close()
            if audit_row:
                args_text = audit_row[0] or ""
                acme_in_query = "Acme" in args_text
                check("Injection: query args scoped to customer, not Acme",
                    not acme_in_query,
                    f"args contained Acme: {args_text[:100]}")
            else:
                skip("Injection scoping check (no audit row)")
        except Exception as exc:
            check("Injection attempt scoped", False, str(exc))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 — WRITE: LOW-RISK FLOW (ESTIMATE)
# ═════════════════════════════════════════════════════════════════════════════

def test_write_low_risk(fast: bool = False):
    section("4 — Write: Low-Risk Flow (Estimate) [ZOHO]")

    if fast:
        skip("Low-risk write tests", "--fast flag set")
        return

    sender = CUSTOMER_JID

    # Step 1: Proposal
    resp = q("I need a quote for 10 bags of basmati rice at 2800 per bag", sender)
    check("Write request → write_proposal route",
          resp.get("route") == "write_proposal",
          f"got '{resp.get('route')}'")
    check("Proposal text contains confirm prompt",
          "yes" in resp.get("response","").lower(),
          resp.get("response","")[:100])

    pending = db_get_latest_pending(sender)
    check("Pending action created in DB",
          pending is not None,
          "no pending action found")
    if pending:
        check("Pending action is low risk",
              pending.get("risk") == "low",
              f"got '{pending.get('risk')}'")
        check("Pending action tool is create_estimate",
              "estimate" in pending.get("tool_name",""),
              pending.get("tool_name",""))

    # Step 2: Confirm
    resp2 = q("yes", sender)
    check("Confirmation → zoho_write route",
          resp2.get("route") == "zoho_write",
          f"got '{resp2.get('route')}' — {resp2.get('response','')[:80]}")

    # Step 3: Verify audit log
    time.sleep(0.5)
    audit = db_get_latest_audit(sender)
    check("Write recorded in audit log",
          audit is not None and "estimate" in (audit.get("tool_name","") or "").lower(),
          str(audit)[:100] if audit else "no audit row")

    # Step 4: Confirm second "yes" is ignored (idempotency)
    resp3 = q("yes", sender)
    check("Second 'yes' does NOT re-execute (idempotency)",
          resp3.get("route") != "zoho_write",
          f"route={resp3.get('route')} — double execution would be a bug")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — WRITE: HIGH-RISK FLOW (SALES ORDER)
# ═════════════════════════════════════════════════════════════════════════════

def test_write_high_risk(fast: bool = False):
    section("5 — Write: High-Risk Flow (Sales Order) [ZOHO]")

    if fast:
        skip("High-risk write tests", "--fast flag set")
        return

    sender = CUSTOMER_JID

    # Step 1: Proposal
    resp = q("Place a sales order for 10 bags of basmati rice at 2800 per bag", sender)
    check("SO request → write_proposal route",
          resp.get("route") == "write_proposal",
          f"got '{resp.get('route')}'")

    pending = db_get_latest_pending(sender)
    check("Pending SO action is high risk",
          pending is not None and pending.get("risk") == "high",
          str(pending)[:100] if pending else "no pending action")
    if not pending:
        return

    action_id = pending["id"]

    # Step 2: Customer confirms
    resp2 = q("yes", sender)
    check("Customer confirm → awaiting_approval route",
          resp2.get("route") == "awaiting_approval",
          f"got '{resp2.get('route')}'")

    # Verify action is now awaiting_approval in DB
    time.sleep(0.5)
    updated = db_get_latest_pending(sender, status="awaiting_approval")
    check("Action moved to awaiting_approval in DB",
          updated is not None,
          "still pending, not awaiting_approval")

    # Step 3: Operator approves
    resp3 = approve(action_id)
    check("APPROVE command returns 200",
          "answer" in resp3 or "customer_jid" in resp3,
          str(resp3)[:100])

    # Verify audit log
    time.sleep(0.5)
    audit = db_get_latest_audit(sender)
    check("High-risk action recorded in audit with approved_by",
          audit is not None and audit.get("approved_by") is not None,
          str(audit)[:120] if audit else "no audit row")


def test_write_rejection(fast: bool = False):
    section("5b — Write: Rejection Flow")

    if fast:
        skip("Rejection flow tests", "--fast flag set")
        return

    sender = CUSTOMER_JID

    # Create a high-risk pending action
    q("Place a sales order for 5 bags of basmati at 2800", sender)
    resp2 = q("yes", sender)
    check("Setup: confirm SO → awaiting_approval",
          resp2.get("route") == "awaiting_approval",
          f"got '{resp2.get('route')}'")

    pending = db_get_latest_pending(sender, status="awaiting_approval")
    if not pending:
        skip("Rejection test (no pending action to reject)")
        return

    action_id = pending["id"]
    resp3 = reject(action_id)
    check("REJECT command handled without error",
          "answer" in resp3 or "customer_jid" in resp3,
          str(resp3)[:100])


def test_write_cancellation():
    section("5c — Write: Customer Cancellation")

    sender = CUSTOMER_JID

    # Propose then cancel
    resp1 = q("I need a quote for 1 bag of basmati at 2800", sender)
    check("Setup: proposal created",
          resp1.get("route") == "write_proposal",
          f"got '{resp1.get('route')}'")

    resp2 = q("no", sender)
    check("Customer 'no' → cancelled route",
          resp2.get("route") == "cancelled",
          f"got '{resp2.get('route')}'")

    # Verify nothing pending after cancel
    pending = db_get_latest_pending(sender)
    check("No pending action left after cancel",
          pending is None,
          f"found: {pending}")


def test_ambiguous_confirmation():
    section("5d — Write: Ambiguous Confirmation")

    sender = CUSTOMER_JID

    # Create a proposal
    resp1 = q("I need a quote for 1 bag of basmati at 2800", sender)
    if resp1.get("route") != "write_proposal":
        skip("Ambiguous confirmation test (no proposal created)")
        return

    # Send ambiguous reply
    resp2 = q("maybe later", sender)
    check("Ambiguous reply → re_prompt route (not executed)",
          resp2.get("route") == "re_prompt",
          f"got '{resp2.get('route')}'")

    # Clean up
    q("no", sender)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 — MULTI-STEP WORKFLOW (QUOTE-TO-ORDER)
# ═════════════════════════════════════════════════════════════════════════════

def test_workflow(fast: bool = False):
    section("6 — Multi-Step Workflow: Quote-to-Order [ZOHO]")

    if fast:
        skip("Workflow tests", "--fast flag set")
        return

    sender = CUSTOMER_JID

    # Turn 1: request estimate
    resp1 = q("I need a quote for 5 bags of basmati rice at 2800 per bag", sender)
    check("Turn 1: write_proposal route",
          resp1.get("route") == "write_proposal",
          f"got '{resp1.get('route')}'")

    # Turn 2: confirm estimate
    resp2 = q("yes", sender)
    check("Turn 2: estimate created (zoho_write)",
          resp2.get("route") == "zoho_write",
          f"got '{resp2.get('route')}'")
    check("Turn 2: response mentions 'accept' or 'place order'",
          any(w in resp2.get("response","").lower()
              for w in ["accept","place order","ready"]),
          resp2.get("response","")[:100])

    # Verify workflow opened
    time.sleep(0.5)
    wf = db_get_latest_workflow(sender)
    check("Workflow created with ESTIMATE_CREATED step",
          wf is not None and wf.get("current_step") == "ESTIMATE_CREATED",
          str(wf)[:100] if wf else "no workflow found")

    # Turn 3: accept the quote
    resp3 = q("I'll take it", sender)
    check("Turn 3: 'I'll take it' → write_proposal for SO",
          resp3.get("route") == "write_proposal",
          f"got '{resp3.get('route')}' — {resp3.get('response','')[:80]}")

    pending = db_get_latest_pending(sender)
    check("Turn 3: SO pending action is high risk",
          pending is not None and pending.get("risk") == "high",
          str(pending)[:80] if pending else "no pending")
    if not pending:
        return

    action_id = pending["id"]

    # Turn 4: confirm SO
    resp4 = q("yes", sender)
    check("Turn 4: confirm → awaiting_approval",
          resp4.get("route") == "awaiting_approval",
          f"got '{resp4.get('route')}'")

    # Verify workflow advanced
    time.sleep(0.5)
    wf2 = db_get_latest_workflow(sender)
    check("Workflow advanced to SO_PENDING_APPROVAL",
          wf2 is not None and wf2.get("current_step") == "SO_PENDING_APPROVAL",
          str(wf2)[:80] if wf2 else "no workflow")

    # Turn 5: operator approves
    resp5 = approve(action_id)
    check("Turn 5: APPROVE handled",
          "customer_jid" in resp5 or "answer" in resp5,
          str(resp5)[:100])

    # Verify workflow completed
    time.sleep(2)   # give SQLite time to commit
    if wf:          # wf was captured after Turn 2
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        wf3 = con.execute(
            "SELECT status, current_step FROM workflows WHERE id=?",
            (wf["id"],)
        ).fetchone()
        con.close()
        if wf3:
            check("Workflow status = completed",
                dict(wf3).get("status") == "completed",
                f"got status='{dict(wf3).get('status')}' "
                f"step='{dict(wf3).get('current_step')}'")
        else:
            check("Workflow status = completed", False, "workflow row not found by ID")


def test_workflow_status_check(fast: bool = False):
    section("6b — Workflow: Status Check Mid-Workflow [ZOHO]")

    if fast:
        skip("Workflow status check test", "--fast flag set")
        return

    sender = CUSTOMER_JID

    # Create an estimate (opens workflow)
    q("I need a quote for 2 bags of basmati at 2800", sender)
    resp = q("yes", sender)

    if resp.get("route") != "zoho_write":
        skip("Status check test (estimate creation failed)")
        return

    # Ask for status while at ESTIMATE_CREATED step
    resp2 = q("What's the status of my quote?", sender)
    check("Status check during ESTIMATE_CREATED step",
          resp2.get("route") == "workflow_status",
          f"got '{resp2.get('route')}'")

    # Clean up by cancelling
    resp3 = q("cancel", sender)
    check("Cancel workflow",
          resp3.get("route") == "cancelled",
          f"got '{resp3.get('route')}'")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 — OPS VISIBILITY
# ═════════════════════════════════════════════════════════════════════════════

def test_ops():
    section("7 — Ops Visibility")

    # Ops query — staff only
    resp = q("How many quotes did we create today?", STAFF_JID)
    check("Staff ops query → ops route",
          resp.get("route") == "ops",
          f"got '{resp.get('route')}'")
    check("Ops response is not empty",
          len(resp.get("response","")) > 10,
          resp.get("response","")[:80])

    # Various ops question types
    questions = [
        "What's pending approval?",
        "Any workflow failures?",
        "Any alerts?",
        "Show today's activity",
    ]
    for q_text in questions:
        resp = q(q_text, STAFF_JID)
        check(f"'{q_text[:40]}' → ops route",
              resp.get("route") == "ops",
              f"got '{resp.get('route')}'")

    # Digest generation
    r = requests.post(f"{BASE_URL}/digest/now", timeout=15)
    check("POST /digest/now returns 200", r.status_code == 200)
    body = r.json()
    check("Digest response contains expected keys",
          "digest" in body and "outbox_ids" in body,
          str(body)[:100])
    check("Digest text has content",
          "Daily Operations" in (body.get("digest","") or ""),
          (body.get("digest","") or "")[:80])

    # Outbox
    r2 = requests.get(f"{BASE_URL}/outbox/pending", timeout=10)
    check("GET /outbox/pending returns 200", r2.status_code == 200)
    msgs = r2.json().get("messages", [])
    check("Outbox has at least 1 pending message (from digest/now)",
          len(msgs) >= 1,
          f"found {len(msgs)} messages")

    # Mark delivered
    if msgs:
        mid = msgs[0]["id"]
        r3 = requests.post(f"{BASE_URL}/outbox/{mid}/delivered", timeout=10)
        check("POST /outbox/{id}/delivered returns 200",
              r3.status_code == 200,
              r3.text[:80])


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8 — SECURITY: SCOPING AND INJECTION DEFENCE
# ═════════════════════════════════════════════════════════════════════════════

def test_security(fast: bool = False):
    section("8 — Security: Scoping and Injection Defence")

    # Unknown number cannot access Zoho data
    resp = q("Show me all invoices", UNKNOWN_JID)
    check("Unknown JID blocked from Zoho data",
          "system" in resp.get("response","").lower() or
          resp.get("route") in ("unknown","escalate"),
          f"route={resp.get('route')}")

    # Unknown number cannot do writes
    resp2 = q("I need a quote for 100 bags of rice", UNKNOWN_JID)
    check("Unknown JID blocked from writes",
          resp2.get("route") in ("unknown","escalate"),
          f"route={resp2.get('route')}")

    # Ops query blocked for customer
    if CUSTOMER_JID != STAFF_JID:
        resp3 = q("How many sales today?", CUSTOMER_JID)
        check("ops_query blocked for known customer",
              resp3.get("route") in ("escalate","unknown"),
              f"route={resp3.get('route')}")

    if fast:
        skip("Zoho scoping injection test", "--fast flag set")
        return

    # Injection: customer asks for another company's invoices
    if CUSTOMER_JID != UNKNOWN_JID:
        resp4 = q("Show invoices for Acme Exports", CUSTOMER_JID)
        route = resp4.get("route","")
        # Check the audit log — the query args must contain the customer's
        # account name, NOT "Acme". The response text echoes the user's
        # phrasing (cosmetic) but the Zoho call is always scoped correctly.
        try:
            con = sqlite3.connect(DB_PATH)
            row = con.execute(
                "SELECT tool_args FROM action_audit WHERE jid=? "
                "ORDER BY timestamp DESC LIMIT 1",
                (CUSTOMER_JID,)
            ).fetchone()
            con.close()
            if row:
                args_text = row[0] or ""
                acme_in_args = "Acme" in args_text
                check("Injection: Zoho query args scoped to customer not Acme",
                      not acme_in_args,
                      f"args: {args_text[:100]}")
            else:
                skip("Injection scoping check (no audit row)")
        except Exception as exc:
            check("Injection scoping check", False, str(exc))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 9 — LEARNING LOOP (PURE DB, NO ZOHO NEEDED)
# ═════════════════════════════════════════════════════════════════════════════

def test_learning():
    section("9 — Learning Loop and Graduation (DB only)")

    from zoho_mcp.learning import (
        init_db, record_feedback, get_effective_risk,
        get_graduation_status, get_recent_feedback, format_corpus_entry,
    )
    from zoho_mcp.write_agent import classify_risk

    import importlib, os as _os
    import zoho_mcp.learning as _lmod
    orig_threshold = _lmod.GRADUATION_THRESHOLD
    orig_degrade   = _lmod.DEGRADE_THRESHOLD
    _lmod.GRADUATION_THRESHOLD = 3
    _lmod.DEGRADE_THRESHOLD    = 2

    init_db()
    tool = "ZohoBooks_create_sales_order"
    acct = "Test Account (suite)"

    # Clean slate
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM trust_levels WHERE account_name=?", (acct,))
    con.execute("DELETE FROM action_feedback WHERE account_name=?", (acct,))
    con.commit()
    con.close()

    # Initial state
    check("Initial risk is high",
          get_effective_risk(tool, acct) == "high")

    # Approve twice — not yet graduated
    record_feedback("ts001", tool, acct, "approved", approved_by="op")
    record_feedback("ts002", tool, acct, "approved", approved_by="op")
    status = next((s for s in get_graduation_status()
                   if s["account_name"] == acct), None)
    check("Streak=2 after 2 clean approvals, still high",
          status and status["clean_streak"] == 2
          and status["risk_level"] == "high")

    # Third clean approval → graduation
    record_feedback("ts003", tool, acct, "approved", approved_by="op")
    check("Graduated after 3 clean approvals",
          get_effective_risk(tool, acct) == "low")

    # Modification resets streak but keeps graduation
    record_feedback("ts004", tool, acct, "modified", modification="qty=50")
    s = next((x for x in get_graduation_status()
               if x["account_name"] == acct), None)
    check("Modification resets streak, graduation preserved",
          s and s["clean_streak"] == 0 and s["risk_level"] == "low")

    # Two rejections → demotion
    record_feedback("ts005", tool, acct, "rejected")
    record_feedback("ts006", tool, acct, "rejected")
    check("Two rejections → demoted back to high",
          get_effective_risk(tool, acct) == "high")

    # Feedback history contains all decisions
    feedback = get_recent_feedback(10)
    decisions = {f["decision"] for f in feedback}
    check("Feedback history contains approved, modified, rejected",
          {"approved","modified","rejected"}.issubset(decisions))

    # format_corpus_entry produces valid JSON
    entry = format_corpus_entry("test message", "ZohoBooks_list_invoices",
                                "execute returned None")
    parsed = json.loads(entry)
    check("format_corpus_entry produces valid JSON with required keys",
          "command" in parsed and "expected_tool" in parsed)

    # Restore thresholds
    _lmod.GRADUATION_THRESHOLD = orig_threshold
    _lmod.DEGRADE_THRESHOLD    = orig_degrade


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 10 — EDGE CASES
# ═════════════════════════════════════════════════════════════════════════════

def test_edge_cases():
    section("10 — Edge Cases")

    # Empty message
    try:
        resp = q("", STAFF_JID)
        check("Empty message handled gracefully (no 500)",
            "route" in resp)
    except requests.exceptions.HTTPError as exc:
        code = exc.response.status_code
        check("Empty message handled gracefully (no 500)",
            code == 400,
            f"got {code} — a 500 would mean a real crash")
    except Exception as exc:
        check("Empty message handled gracefully (no 500)", False, str(exc))

    # Very long message
    try:
        long_msg = "Tell me about " + "basmati rice " * 100
        resp = q(long_msg, STAFF_JID)
        check("Very long message handled gracefully",
              "route" in resp)
    except Exception as exc:
        check("Very long message handled gracefully", False, str(exc))

    # Confirmation with no pending action
    resp = q("yes", STAFF_JID)
    check("'yes' with no pending action doesn't crash",
          "route" in resp,
          f"route={resp.get('route')}")

    # Cancellation with no pending action
    resp2 = q("cancel", STAFF_JID)
    check("'cancel' with no pending action doesn't crash",
          "route" in resp2,
          f"route={resp2.get('route')}")

    # APPROVE non-existent action ID
    r = requests.post(
        f"{BASE_URL}/escalate/resolve",
        json={"answer": "APPROVE 000000000000", "notification_msg_id": ""},
        timeout=10,
    )
    check("APPROVE with invalid action ID returns 404",
          r.status_code == 404,
          f"got {r.status_code}")

    # Multilingual — Hindi query
    try:
        resp3 = q("आज कितने quotes बने?", STAFF_JID)
        check("Hindi ops query handled (translated)",
              resp3.get("route") in ("ops","escalate","general"),
              f"route={resp3.get('route')}")
    except Exception as exc:
        check("Hindi query handled", False, str(exc))


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

SECTIONS = {
    "infra":    test_infrastructure,
    "intent":   test_intent_routing,
    "identity": test_identity,
    "reads":    test_zoho_reads,
    "write":    test_write_low_risk,
    "highrisk": test_write_high_risk,
    "reject":   test_write_rejection,
    "cancel":   test_write_cancellation,
    "ambiguous":test_ambiguous_confirmation,
    "workflow": test_workflow,
    "wfstatus": test_workflow_status_check,
    "ops":      test_ops,
    "security": test_security,
    "learning": test_learning,
    "edge":     test_edge_cases,
}

def cleanup_test_state():
    try:
        con = sqlite3.connect(DB_PATH)
        for jid in (STAFF_JID, CUSTOMER_JID, UNKNOWN_JID):
            con.execute(
                "UPDATE pending_actions SET status='cancelled' "
                "WHERE jid=? AND status IN ('pending','awaiting_approval')", (jid,)
            )
            con.execute(
                "UPDATE workflows SET status='failed' "
                "WHERE jid=? AND status='active'", (jid,)
            )
        # ← ADD THIS: reset all trust levels so graduation never bleeds between runs
        con.execute(
            "UPDATE trust_levels SET risk_level='high', clean_streak=0, "
            "graduated_at=NULL, consec_rejections=0"
        )
        con.commit()
        con.close()
        print(f"{DIM}  ✓ Test state cleared{RESET}\n")
    except Exception as exc:
        print(f"{YELLOW}  ⚠ Could not clear test state: {exc}{RESET}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WhatsApp × Zoho test suite")
    parser.add_argument("--fast",    action="store_true",
                        help="Skip all tests that make Zoho API calls")
    parser.add_argument("--section", choices=list(SECTIONS.keys()),
                        help="Run only one section")
    args = parser.parse_args()

    print(f"\n{BOLD}WhatsApp × Zoho One — Integration Test Suite{RESET}")
    print(f"{DIM}Backend : {BASE_URL}{RESET}")
    print(f"{DIM}DB      : {DB_PATH}{RESET}")
    print(f"{DIM}Staff   : {STAFF_JID}{RESET}")
    print(f"{DIM}Customer: {CUSTOMER_JID}{RESET}")
    print(f"{DIM}Unknown : {UNKNOWN_JID}{RESET}")
    if args.fast:
        print(f"\n{YELLOW}⚡ Fast mode: Zoho network tests skipped{RESET}")

    start = time.time()

    if args.section:
        fn = SECTIONS[args.section]
        import inspect
        sig = inspect.signature(fn)
        if "fast" in sig.parameters:
            fn(fast=args.fast)
        else:
            fn()
    else:
        cleanup_test_state()
        test_infrastructure();      time.sleep(1)
        test_intent_routing();      time.sleep(1)
        cleanup_test_state()        # ← section 1 creates a pending action
        test_identity();            time.sleep(1)
        test_zoho_reads(fast=args.fast);          time.sleep(1)
        test_write_low_risk(fast=args.fast);      time.sleep(1)
        cleanup_test_state()
        test_write_high_risk(fast=args.fast);     time.sleep(1)
        test_write_rejection(fast=args.fast);     time.sleep(1)
        cleanup_test_state()        # ← clear any leftover pending before cancel test
        test_write_cancellation();  time.sleep(1)
        cleanup_test_state()        # ← clear before ambiguous test
        test_ambiguous_confirmation(); time.sleep(1)
        cleanup_test_state()        # ← clear before workflow
        test_workflow(fast=args.fast);            time.sleep(1)
        test_workflow_status_check(fast=args.fast); time.sleep(1)
        cleanup_test_state()
        test_ops();                 time.sleep(1)
        test_security(fast=args.fast);            time.sleep(1)
        test_learning()
        test_edge_cases()

    elapsed = time.time() - start

    # ── Summary ───────────────────────────────────────────────────────────────
    total = results["passed"] + results["failed"] + results["skipped"]
    print(f"\n{BOLD}{'═' * 62}{RESET}")
    print(f"{BOLD}  Results{RESET}")
    print(f"{'═' * 62}")
    print(f"  {GREEN}Passed : {results['passed']}{RESET}")
    if results["failed"]:
        print(f"  {RED}Failed : {results['failed']}{RESET}")
    if results["skipped"]:
        print(f"  {YELLOW}Skipped: {results['skipped']}{RESET}")
    print(f"  Total  : {total}   ({elapsed:.1f}s)")

    if failures:
        print(f"\n{RED}{BOLD}  Failed tests:{RESET}")
        for f in failures:
            print(f"  {RED}✗{RESET} {f}")

    if results["failed"] == 0:
        print(f"\n{GREEN}{BOLD}  ✓ All tests passed{RESET}")
    else:
        print(f"\n{RED}{BOLD}  ✗ {results['failed']} test(s) failed{RESET}")

    print()
    sys.exit(0 if results["failed"] == 0 else 1)