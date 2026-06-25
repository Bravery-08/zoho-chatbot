# backend/zoho_mcp/intent.py
"""
Four-way intent classifier for the Zoho agent pipeline.

Sits between the query rewriter and the RAG / Zoho / general paths in main.py.
Uses the fast 8B model — this is a cheap routing call, not an answer.

Intents
───────
answer_from_kb  — question about company policy, SOPs, product catalogue,
                  payment terms, shipping procedures, or anything answerable
                  from the ChromaDB knowledge base without live Zoho data.

read_zoho       — question requiring live data from Zoho One:
                  order / shipment status, invoice lists, customer balances,
                  stock levels, lead / deal / contact lookups, purchase orders.

general         — off-topic, general knowledge, greetings, thanks,
                  calculations, translations, or anything unrelated to business.

escalate        — business-related but requires human judgment:
                  complaints, pricing negotiations, credit decisions,
                  or anything neither the KB nor Zoho data can resolve.
"""
import json
import logging

from groq import Groq

from zoho_mcp.config import GROQ_API_KEY, INTENT_MODEL

log = logging.getLogger(__name__)

INTENTS = ("answer_from_kb", "read_zoho", "general", "escalate")

_SYSTEM = """
You classify customer messages for a B2B merchant-export company chatbot.

Output a single JSON object: {"intent": "<value>"}
Value must be exactly one of:

  answer_from_kb — question about company policy, SOPs, product catalogue,
                   payment terms, export procedures, shipping rules, or anything
                   answerable from a static knowledge base without live data.

  read_zoho      — question requiring live Zoho data:
                   order/shipment/invoice status, customer balance, stock levels,
                   lead/deal/contact lookups, purchase orders, sales orders,
                   estimates, or any query that needs current records.

  general        — off-topic, general knowledge, greetings, thanks, weather,
                   exchange rates, calculations, translations, or anything
                   unrelated to the company's business data.

  escalate       — business-related but needs a human: complaints, credit
                   decisions, pricing negotiations, or questions neither the
                   knowledge base nor live Zoho data can answer.

Output ONLY valid JSON. No explanation. No markdown. No preamble.
Example: {"intent": "read_zoho"}
""".strip()

# Module-level client — one instance shared across all calls.
_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=GROQ_API_KEY)
    return _client


def classify(message: str, history: list[dict]) -> str:
    """
    Classify a rewritten message into one of four intents.

    Parameters
    ----------
    message : str
        The rewritten (context-resolved) user message in English.
    history : list[dict]
        Conversation history as {"role": ..., "content": ...} dicts.
        Last 4 turns are sent for context; full history is ignored to keep
        the prompt small and the call fast.

    Returns
    -------
    str
        One of: "answer_from_kb" | "read_zoho" | "general" | "escalate".
        Defaults to "escalate" on any error — humans should always get the
        message if the classifier fails.
    """
    client = _get_client()

    # Keep only the most recent 4 turns so the fast model stays within budget.
    recent = history[-4:] if len(history) > 4 else history
    messages = [{"role": "system", "content": _SYSTEM}]
    messages.extend(recent)
    messages.append({"role": "user", "content": message})

    try:
        resp = client.chat.completions.create(
            model=INTENT_MODEL,
            messages=messages,
            temperature=0,
            max_tokens=24,
        )
        raw = resp.choices[0].message.content.strip()
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