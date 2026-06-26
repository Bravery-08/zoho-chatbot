# backend/zoho_mcp/intent.py
"""
Five-way intent classifier.

answer_from_kb — policy, SOPs, product catalogue, payment terms: ChromaDB
read_zoho      — live Zoho data query: orders, invoices, contacts, stock
write_zoho     — action that would create/update a Zoho record
general        — off-topic, greetings, calculations, translations
escalate       — needs a human: complaints, negotiations, credit decisions
"""
import json
import logging

from groq import Groq

from zoho_mcp.config import GROQ_API_KEY, INTENT_MODEL

log = logging.getLogger(__name__)

INTENTS = ("answer_from_kb", "read_zoho", "write_zoho", "general", "escalate")

_SYSTEM = """
You classify customer messages for a B2B merchant-export company chatbot.

Output a single JSON object: {"intent": "<value>"}
Value must be exactly one of:

  answer_from_kb — question about company policy, SOPs, product catalogue,
                   payment terms, export procedures, or anything answerable
                   from a static knowledge base without live data.

  read_zoho      — question requiring live Zoho data:
                   order/invoice/shipment status, customer balance, stock levels,
                   lead/deal/contact lookups, purchase orders, estimates.

  write_zoho     — request to CREATE or UPDATE a Zoho record:
                   "I need a quote for...", "place an order for...",
                   "create an enquiry for...", "send me an estimate for...",
                   "I want to order...", "can you raise a PO for...".

  general        — off-topic, greetings, thanks, weather, exchange rates,
                   calculations, translations, or unrelated to business data.

  escalate       — needs a human: complaints, credit decisions, pricing
                   negotiations, refunds, or questions neither KB nor Zoho
                   data can resolve.

Output ONLY valid JSON. No explanation. No markdown. No preamble.
Example: {"intent": "write_zoho"}
""".strip()

_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=GROQ_API_KEY)
    return _client


def classify(message: str, history: list[dict]) -> str:
    """
    Classify a rewritten message into one of five intents.
    Defaults to 'escalate' on any error.
    """
    client = _get_client()
    recent = history[-4:] if len(history) > 4 else history
    messages = [{"role": "system", "content": _SYSTEM}]
    messages.extend(recent)
    messages.append({"role": "user", "content": message})

    try:
        resp = client.chat.completions.create(
            model=INTENT_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=24,
        )
        raw    = resp.choices[0].message.content.strip()
        parsed = json.loads(raw)
        intent = parsed.get("intent", "escalate")
        if intent not in INTENTS:
            log.warning("[intent] unknown value '%s' — defaulting to escalate", intent)
            return "escalate"
        log.info("[intent] %s | '%s'", intent, message[:80])
        return intent
    except Exception as exc:
        log.error("[intent] classification failed (%s) — defaulting to escalate", exc)
        return "escalate"