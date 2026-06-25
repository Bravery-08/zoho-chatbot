# backend/zoho_mcp/identity.py
"""
Phase 2 — Identity resolution.

Maps an incoming WhatsApp JID to one of three states:

  internal  — JID is on the STAFF_JIDS whitelist.
               Full tool access, no scoping restrictions.

  known     — JID matches a Zoho CRM Contact by phone number.
               Data access scoped to their account only.

  unknown   — JID not found in CRM.
               No Zoho data access; KB/general questions still allowed.

Resolution is cached for IDENTITY_CACHE_TTL seconds (default 1 hour)
so CRM is only queried on the first message from a given number.

Call identity.invalidate(jid) to force re-resolution (e.g. after you
manually link a new customer's phone number in CRM).
"""
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from zoho_mcp.client import ZohoMCPClient, result_to_text

log = logging.getLogger(__name__)

# ── Staff whitelist ───────────────────────────────────────────────────────────
# STAFF_JIDS: comma-separated phone numbers WITH country code, no + or spaces.
# Example: STAFF_JIDS=919876543210,918765432109
# These are matched against the digits-only portion of the incoming JID.
_STAFF_PHONES: set[str] = {
    p.strip()
    for p in os.getenv("STAFF_JIDS", "").split(",")
    if p.strip().isdigit()
}

# ── Identity cache ────────────────────────────────────────────────────────────
_CACHE_TTL: int = int(os.getenv("IDENTITY_CACHE_TTL", "3600"))   # seconds
_cache: dict[str, tuple["CustomerIdentity", float]] = {}


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class CustomerIdentity:
    """
    Resolved identity for one WhatsApp sender.

    Attributes
    ----------
    state        : "internal" | "known" | "unknown"
    jid          : full WhatsApp JID  (e.g. 919876543210@s.whatsapp.net)
    phone        : digits-only portion (e.g. 919876543210)
    contact_name : CRM Contact full name (known only)
    account_name : CRM Account name — doubles as Zoho Books customer_name (known only)
    contact_id   : CRM Contact record ID (known only)
    """
    state:        str
    jid:          str
    phone:        str
    contact_name: Optional[str] = None
    account_name: Optional[str] = None
    contact_id:   Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_phone(jid: str) -> str:
    """Strip @s.whatsapp.net / @c.us suffix → plain digits."""
    return jid.split("@")[0].strip()


def _cache_get(jid: str) -> Optional[CustomerIdentity]:
    entry = _cache.get(jid)
    if entry:
        identity, ts = entry
        if time.time() - ts < _CACHE_TTL:
            return identity
        del _cache[jid]
    return None


def _cache_set(identity: CustomerIdentity) -> None:
    _cache[identity.jid] = (identity, time.time())


def invalidate(jid: str) -> None:
    """Force re-resolution on the next message from this JID."""
    _cache.pop(jid, None)
    log.info("[identity] cache invalidated for %s", jid)


# ── CRM lookup ────────────────────────────────────────────────────────────────

async def _crm_search_phone(phone: str) -> Optional[dict]:
    """
    Search Zoho CRM Contacts by phone number.
    Tries the full number first (with country code), then the last 10 digits.
    Returns the first matching Contact record dict, or None.
    """
    candidates = [phone]
    if len(phone) > 10:
        candidates.append(phone[-10:])   # strip country code as fallback

    for number in candidates:
        try:
            async with ZohoMCPClient() as zoho:
                result = await zoho.call_tool("ZohoCRM_searchRecords", {
                    "path_variables": {"module": "Contacts"},
                    "query_params":   {"phone": number},
                })
            text = result_to_text(result)
            data = json.loads(text)
            records = data.get("data") or []
            if records:
                log.info("[identity] CRM hit for phone=%s (tried %s)", phone, number)
                return records[0]
        except Exception as exc:
            log.warning("[identity] CRM search failed for %s: %s", number, exc)

    return None


# ── Main entry point ──────────────────────────────────────────────────────────

async def resolve(jid: str) -> CustomerIdentity:
    """
    Resolve a WhatsApp JID to a CustomerIdentity.

    Priority order:
      1. Staff whitelist  → internal (no CRM call needed)
      2. In-memory cache  → return cached result
      3. CRM phone search → known or unknown (cached afterward)

    On any CRM error, defaults to "unknown" so the pipeline degrades
    gracefully rather than blocking all messages.
    """
    phone = extract_phone(jid)

    # 1. Staff whitelist — checked before cache so staff can't be overridden
    if phone in _STAFF_PHONES:
        identity = CustomerIdentity(state="internal", jid=jid, phone=phone)
        log.info("[identity] internal | phone=%s", phone)
        return identity

    # 2. Cache
    cached = _cache_get(jid)
    if cached:
        log.info("[identity] cache hit | state=%s account=%s",
                 cached.state, cached.account_name)
        return cached

    # 3. CRM lookup
    record = await _crm_search_phone(phone)

    if record:
        first = record.get("First_Name", "")
        last  = record.get("Last_Name",  "")
        contact_name = f"{first} {last}".strip() or None

        account_raw  = record.get("Account_Name")
        account_name = (
            account_raw.get("name") if isinstance(account_raw, dict)
            else account_raw
        ) or None

        identity = CustomerIdentity(
            state        = "known",
            jid          = jid,
            phone        = phone,
            contact_name = contact_name,
            account_name = account_name,
            contact_id   = record.get("id"),
        )
        log.info("[identity] known | name=%s account=%s",
                 identity.contact_name, identity.account_name)
    else:
        identity = CustomerIdentity(state="unknown", jid=jid, phone=phone)
        log.info("[identity] unknown | phone=%s", phone)

    _cache_set(identity)
    return identity