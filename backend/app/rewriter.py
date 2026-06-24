# backend/app/rewriter.py
import logging
from groq import Groq as GroqClient
from app.config import GROQ_API_KEY, REWRITER_MODEL_NAME, REWRITER_SYSTEM_PROMPT

log = logging.getLogger(__name__)

_client: GroqClient | None = None

MAX_QUOTED_LENGTH = 300  # truncate long quoted messages (e.g. verbose bot responses)


def _get_client() -> GroqClient:
    global _client
    if _client is None:
        _client = GroqClient(api_key=GROQ_API_KEY)
    return _client


def rewrite_query(message: str, history: list, quoted_text: str = None) -> str:
    """
    Rewrite an incomplete or context-dependent query into a fully
    self-contained one.

    Priority order for context:
    1. quoted_text  — the specific message the user is replying to (highest weight)
    2. history      — the broader conversation context
    3. message      — the current raw input

    Short-circuits and returns the original message if:
    - both history and quoted_text are absent (nothing to draw from)
    - the LLM call fails (safe fallback)
    """
    if not history and not quoted_text:
        return message

    client = _get_client()

    # ── Build user prompt ─────────────────────────────────────────────────────
    sections = []

    if history:
        history_text = "\n".join(
            f"{'User' if h.role == 'user' else 'Assistant'}: {h.content}"
            for h in history
        )
        sections.append(f"Conversation so far:\n{history_text}")

    if quoted_text:
        truncated = quoted_text[:MAX_QUOTED_LENGTH]
        if len(quoted_text) > MAX_QUOTED_LENGTH:
            truncated += "..."
        sections.append(
            f"The user is directly replying to this specific message:\n"
            f"\"{truncated}\""
        )

    sections.append(f"Current message: {message}")
    user_prompt = "\n\n".join(sections)

    try:
        completion = client.chat.completions.create(
            model=REWRITER_MODEL_NAME,
            messages=[
                {"role": "system", "content": REWRITER_SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=80,
            temperature=0,
        )
        rewritten = completion.choices[0].message.content.strip()

        if rewritten != message:
            log.info(f"  [rewriter] '{message}' → '{rewritten}'")
        else:
            log.info(f"  [rewriter] unchanged")

        return rewritten

    except Exception as e:
        log.warning(f"  [rewriter] failed ({e}) — using original message")
        return message