# backend/app/judge.py
import logging
from typing import List
from groq import Groq as GroqClient
from app.config import GROQ_API_KEY, JUDGE_MODEL_NAME, JUDGE_SYSTEM_PROMPT

log = logging.getLogger(__name__)

_client: GroqClient | None = None


def _get_client() -> GroqClient:
    global _client
    if _client is None:
        _client = GroqClient(api_key=GROQ_API_KEY)
    return _client


def is_sufficient(query: str, nodes: List) -> bool:
    """
    Ask the LLM whether the retrieved chunks contain enough information
    to answer the query.

    Returns:
        True  — chunks are sufficient; caller should synthesize an answer.
        False — chunks are insufficient; caller should classify and escalate/deflect.

    Defaults to False on any unexpected output or exception, so ambiguous
    cases fall through to escalation rather than producing a bad answer.
    """
    if not nodes:
        log.info("  [judge] no nodes retrieved — insufficient by default")
        return False

    # Build a single string of all chunk texts, numbered for clarity
    chunks_text = "\n\n".join(
        f"Chunk {i + 1}:\n{n.text.strip()}"
        for i, n in enumerate(nodes)
    )

    user_message = (
        f"Query: {query}\n\n"
        f"Retrieved chunks:\n{chunks_text}"
    )

    client = _get_client()

    try:
        completion = client.chat.completions.create(
            model=JUDGE_MODEL_NAME,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
            max_tokens=5,
            temperature=0,
        )
        raw = completion.choices[0].message.content.strip().lower()
        log.info(f"  [judge] raw_response='{raw}'")

        if "sufficient" in raw and "insufficient" not in raw:
            return True
        return False  # default on ambiguous output

    except Exception as e:
        log.warning(f"  [judge] is_sufficient failed ({e}) — defaulting to False")
        return False