# backend/app/general.py
import logging
from groq import Groq as GroqClient
from app.config import (
    GROQ_API_KEY,
    MODEL_NAME,
    GENERAL_LLM_SYSTEM_PROMPT,
    GENERAL_LLM_MAX_TOKENS,
    LIFECYCLE_MODEL_NAME,
    LIFECYCLE_SYSTEM_PROMPT,
)

log = logging.getLogger(__name__)

_client: GroqClient | None = None

# Detected in Python — no LLM call needed for these
# These get no Part 1 response, only the lifecycle follow-up question
_FILLER_ACKNOWLEDGMENTS = {
    "ok", "okay", "done", "cool", "got it", "alright", "sure", "noted",
    "i see", "fine", "great", "nice", "sounds good", "understood", "right",
    "yep", "yup", "yeah", "k", "kk", "roger", "oki", "okie", "ack",
    # Affirmatives — mean "I want more help", skip main LLM, go straight to lifecycle
    "yes",
}


def _get_client() -> GroqClient:
    global _client
    if _client is None:
        _client = GroqClient(api_key=GROQ_API_KEY)
    return _client


def _is_filler(message: str) -> bool:
    """Check if the message is a short filler acknowledgment that needs no reply."""
    normalized = message.strip().lower().rstrip('!.?,')
    return normalized in _FILLER_ACKNOWLEDGMENTS


def _generate_main_response(message: str, history: list) -> str:
    """Call 1 — respond to the user's message only."""
    messages = [{"role": "system", "content": GENERAL_LLM_SYSTEM_PROMPT}]
    for h in history:
        messages.append({"role": h.role, "content": h.content})
    messages.append({"role": "user", "content": message})

    completion = _get_client().chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        max_tokens=GENERAL_LLM_MAX_TOKENS,
    )
    return completion.choices[0].message.content.strip()


def _generate_follow_up(message: str, history: list) -> str | None:
    """
    Call 2 — lifecycle state machine.
    Returns a follow-up question string, or None if conversation is ending.
    """
    history_text = "\n".join(
        f"{'User' if h.role == 'user' else 'Assistant'}: {h.content}"
        for h in history
    ) if history else "(no prior conversation)"

    user_prompt = (
        f"Conversation history:\n{history_text}\n\n"
        f"User's latest message: {message}"
    )

    completion = _get_client().chat.completions.create(
        model=LIFECYCLE_MODEL_NAME,
        messages=[
            {"role": "system", "content": LIFECYCLE_SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        max_tokens=60,
        temperature=0,
    )
    result = completion.choices[0].message.content.strip()
    log.info(f"  [lifecycle] state_output='{result}'")

    return None if result.strip().upper() == "NONE" else result


def general_llm_answer(message: str, history: list) -> str:
    log.info(f"  [general] query='{message[:80]}'")

    if _is_filler(message):
        # Short filler — skip main LLM call entirely, go straight to follow-up
        log.info(f"  [general] filler detected — skipping main response")
        follow_up = _generate_follow_up(message, history)
        final = follow_up if follow_up else ""
    else:
        main_response = _generate_main_response(message, history)
        log.info(f"  [general] main='{main_response[:80]}'")
        follow_up = _generate_follow_up(message, history)

        if follow_up:
            final = f"{main_response} {follow_up}"
        else:
            final = main_response

    log.info(f"  [general] final='{final[:100]}'")
    return final