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
# After (accepts phone numbers AND full JIDs like 141407654273204@lid):
_raw_staff    = [p.strip() for p in os.getenv("STAFF_JIDS", "").split(",") if p.strip()]
_STAFF_PHONES: set[str] = {p for p in _raw_staff if p.isdigit()}   # 917977909705
_STAFF_JIDS:   set[str] = set(_raw_staff)                           # full JIDs too

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
    state:             str
    jid:               str
    phone:             str
    contact_name:      Optional[str] = None
    account_name:      Optional[str] = None
    contact_id:        Optional[str] = None   # CRM Contact record ID
    books_customer_id: Optional[str] = None   # Zoho Books contact_id (for writes)


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


# ── Books customer_id lookup ──────────────────────────────────────────────────

async def _books_lookup_customer_id(account_name: str) -> Optional[str]:
    """
    Resolve the Zoho Books contact_id for a CRM account name.

    Checks the manual BOOKS_CUSTOMER_ID_MAP first (instant, no network call),
    then falls back to the ZohoBooks_list_contacts API.
    """
    from zoho_mcp.config import ZOHO_ORG_ID
    if not account_name:
        return None

    # 2. Live API lookup via ZohoBooks_list_contacts
    if not ZOHO_ORG_ID:
        return None
    try:
        async with ZohoMCPClient() as zoho:
            result = await zoho.call_tool("ZohoBooks_list_contacts", {
                "query_params": {
                    "organization_id": ZOHO_ORG_ID,
                    "contact_name":    account_name,
                }
            })
        text     = result_to_text(result)
        data     = json.loads(text)
        contacts = data.get("contacts") or []
        if contacts:
            cid = contacts[0].get("contact_id")
            log.info("[identity] Books customer_id=%s via API for '%s'", cid, account_name)
            return cid
    except Exception as exc:
        log.warning("[identity] Books customer_id API lookup failed for '%s': %s",
                    account_name, exc)
    return None

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
    if phone in _STAFF_PHONES or jid in _STAFF_JIDS:
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

        # Look up the Zoho Books contact_id for write operations.
        # This runs once per customer per cache-TTL window — the result is
        # stored on the identity so execute_write() can inject it without
        # making an extra network call.
        books_customer_id = await _books_lookup_customer_id(account_name) if account_name else None

        identity = CustomerIdentity(
            state             = "known",
            jid               = jid,
            phone             = phone,
            contact_name      = contact_name,
            account_name      = account_name,
            contact_id        = record.get("id"),
            books_customer_id = books_customer_id,
        )
        log.info("[identity] known | name=%s account=%s books_id=%s",
                 identity.contact_name, identity.account_name, identity.books_customer_id)
    else:
        identity = CustomerIdentity(state="unknown", jid=jid, phone=phone)
        log.info("[identity] unknown | phone=%s", phone)

    _cache_set(identity)
    return identity