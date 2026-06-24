# backend/app/router.py
import logging
from groq import Groq as GroqClient
from app.config import GROQ_API_KEY, ROUTER_MODEL_NAME, ROUTER_SYSTEM_PROMPT

log = logging.getLogger(__name__)

# Module-level client — one instance, reused for every routing call
_client: GroqClient | None = None


def _get_client() -> GroqClient:
    global _client
    if _client is None:
        _client = GroqClient(api_key=GROQ_API_KEY)
    return _client


def classify_query(message: str) -> str:
    """
    Calls a small, fast LLM to classify the message as 'company' or 'general'.

    Returns:
        'company'  — query is about the company; route to RAG pipeline.
        'general'  — query is general knowledge or off-topic; route to general LLM.

    On any unexpected output or exception, defaults to 'company' so ambiguous
    messages attempt RAG before escalating rather than silently going to the
    general LLM.
    """
    client = _get_client()

    try:
        completion = client.chat.completions.create(
            model=ROUTER_MODEL_NAME,
            messages=[
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                {"role": "user",   "content": message},
            ],
            max_tokens=5,    # we only need one word back
            temperature=0,   # fully deterministic
        )
        raw = completion.choices[0].message.content.strip().lower()
        log.info(f"  [router] raw_response='{raw}'")

        if "general" in raw:
            return "general"
        return "company"   # default on ambiguous output

    except Exception as e:
        log.warning(f"  [router] classify_query failed ({e}) — defaulting to 'company'")
        return "company"