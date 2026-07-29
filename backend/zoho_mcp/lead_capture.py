# backend/zoho_mcp/lead_capture.py
"""
Phase B — Lead capture for unknown WhatsApp callers.

When someone messages from a number not in CRM, instead of dead-ending
them with "not in our system", the bot runs a two-turn capture flow:

  Turn 1 (any message from unknown caller)
    → Store their first message
    → Ask: "Could you share your name and company?"

  Turn 2 (their reply)
    → LLM extracts name + company
    → Create CRM Lead with their phone, name, company, first message
    → Notify operator via outbox
    → Tell customer: "Thanks — our team will follow up shortly."

  Turn 3+ (same number, still unknown — lead was created but not yet
            linked as a CRM Contact with the phone number)
    → Politely note that details are on file, KB/general only

When the operator adds this person to CRM as a Contact with their phone
number, the identity resolution will start returning state='known' and
they'll get full customer access automatically.

The state table lives in WRITE_ACTIONS_DB_PATH alongside all other
write-related state (audit, workflows, pending actions, etc.)
"""
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from groq import Groq

from zoho_mcp.client import ZohoMCPClient, result_to_text
from zoho_mcp.config import GROQ_API_KEY, INTENT_MODEL, ZOHO_WRITE_MCP_URL, ZOHO_ORG_ID

log = logging.getLogger(__name__)

DB_PATH = os.getenv("WRITE_ACTIONS_DB_PATH", "./data/write_actions.db")

WELCOME_MESSAGE = (
    "Welcome! I couldn't find your number in our system. "
    "Could you share your *name* and *company* so I can help you better?"
)

CAPTURED_MESSAGE = (
    "Thanks {name}! I've noted your details. "
    "Our team will be in touch shortly. "
    "Feel free to ask any general questions in the meantime."
)

ALREADY_CAPTURED_MESSAGE = (
    "Your details are already on file — our team will follow up with you soon. "
    "I'm happy to help with any general questions."
)


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class LeadCaptureState:
    jid:           str
    phone:         str
    first_message: str
    state:         str   # awaiting_info | created | failed
    crm_lead_id:   Optional[str]
    captured_at:   float


# ── DB setup ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS lead_capture (
            jid           TEXT PRIMARY KEY,
            phone         TEXT NOT NULL,
            first_message TEXT NOT NULL,
            state         TEXT NOT NULL DEFAULT 'awaiting_info',
            crm_lead_id   TEXT,
            captured_at   REAL NOT NULL
        )
    """)
    con.commit()
    con.close()
    log.info("[lead_capture] table ready in %s", DB_PATH)


# ── CRUD ──────────────────────────────────────────────────────────────────────

def get_state(jid: str) -> Optional[LeadCaptureState]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM lead_capture WHERE jid=?", (jid,)
    ).fetchone()
    con.close()
    if not row:
        return None
    return LeadCaptureState(
        jid=row["jid"], phone=row["phone"],
        first_message=row["first_message"], state=row["state"],
        crm_lead_id=row["crm_lead_id"], captured_at=row["captured_at"],
    )


def set_awaiting_info(jid: str, phone: str, first_message: str) -> None:
    """Record that we've asked this unknown caller for their details."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR IGNORE INTO lead_capture "
        "(jid, phone, first_message, state, captured_at) VALUES (?,?,?,?,?)",
        (jid, phone, first_message, "awaiting_info", time.time()),
    )
    con.commit()
    con.close()
    log.info("[lead_capture] awaiting_info for %s", jid)


def mark_created(jid: str, crm_lead_id: str) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE lead_capture SET state='created', crm_lead_id=? WHERE jid=?",
        (crm_lead_id, jid),
    )
    con.commit()
    con.close()
    log.info("[lead_capture] lead created=%s for %s", crm_lead_id, jid)


def mark_failed(jid: str) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE lead_capture SET state='failed' WHERE jid=?", (jid,)
    )
    con.commit()
    con.close()
    log.warning("[lead_capture] lead creation failed for %s", jid)


# ── LLM extraction ────────────────────────────────────────────────────────────

async def extract_name_company(message: str) -> tuple[str, str]:
    """
    Extract the person's name and company from their self-introduction.
    Returns (name, company). Falls back to ("Unknown", "") on failure.
    """
    client = Groq(api_key=GROQ_API_KEY)
    try:
        resp = client.chat.completions.create(
            model=INTENT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract the person's name and company from their message. "
                        "Output ONLY a JSON object: "
                        '{"name": "...", "company": "..."}\n'
                        "If name not found, use 'Unknown'. "
                        "If company not found, use empty string."
                    ),
                },
                {"role": "user", "content": message},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=60,
        )
        data = json.loads(resp.choices[0].message.content)
        name    = (data.get("name", "") or "Unknown").strip()
        company = (data.get("company", "") or "").strip()
        log.info("[lead_capture] extracted name='%s' company='%s'", name, company)
        return name, company
    except Exception as exc:
        log.error("[lead_capture] extraction failed: %s", exc)
        return "Unknown", ""


# ── CRM lead creation ─────────────────────────────────────────────────────────

async def create_crm_lead(
    phone:         str,
    name:          str,
    company:       str,
    first_message: str,
) -> Optional[str]:
    """
    Create a CRM Lead record via the write MCP server.
    Returns the new Lead's record ID, or None on failure.
    """

    lead_data = {
        "Last_Name":   name,
        "Company":     company or "Unknown",
        "Phone":       phone,
        "Lead_Source": "WhatsApp",
        "Description": f"First WhatsApp enquiry:\n{first_message[:500]}",
    }

    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool("ZohoCRM_createRecords", {
                "path_variables": {"module": "Leads"},
                "body":           {"data": [lead_data]},
            })
        text = result_to_text(result)
        log.info("[lead_capture] CRM response (first 200): %s", text[:200])

        data    = json.loads(text)
        records = data.get("data", [])
        if records and records[0].get("code") == "SUCCESS":
            lead_id = records[0].get("details", {}).get("id", "")
            log.info("[lead_capture] Lead created id=%s name=%s company=%s",
                     lead_id, name, company)
            return lead_id
        else:
            log.error("[lead_capture] Zoho rejected lead creation: %s", text[:200])
            return None
    except Exception as exc:
        log.error("[lead_capture] create_crm_lead failed: %s", exc)
        return None


# ── Main flow handler ─────────────────────────────────────────────────────────

async def handle_unknown_caller(
    jid:     str,
    phone:   str,
    message: str,
) -> tuple[str, bool]:
    """
    Handle a message from an unknown caller (not in CRM).

    Returns (response_text, lead_was_just_created).

    The caller handles notifying the operator if lead_was_just_created=True.
    """
    state = get_state(jid)

    # First contact — no state yet
    if state is None:
        set_awaiting_info(jid, phone, message)
        return WELCOME_MESSAGE, False

    # Second contact — they're replying with their details
    if state.state == "awaiting_info":
        name, company = await extract_name_company(message)
        lead_id = await create_crm_lead(phone, name, company, state.first_message)

        if lead_id:
            mark_created(jid, lead_id)
            response = CAPTURED_MESSAGE.format(name=name)
            return response, True
        else:
            mark_failed(jid)
            response = (
                "Thanks for sharing your details. "
                "I had trouble saving them — our team will reach out to you directly."
            )
            return response, False

    # Already captured — they're messaging again
    return ALREADY_CAPTURED_MESSAGE, False