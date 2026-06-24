# backend/app/translator.py
import json
import logging
import re
from groq import Groq as GroqClient
from app.config import GROQ_API_KEY, TRANSLATOR_MODEL_NAME

log = logging.getLogger(__name__)

_client: GroqClient | None = None


def _get_client() -> GroqClient:
    global _client
    if _client is None:
        _client = GroqClient(api_key=GROQ_API_KEY)
    return _client


def detect_and_translate(text: str) -> tuple[str, str, str]:
    """
    Detect the language and script of text and translate to English.

    Returns:
        (english_text, source_language, source_script)

        source_language: e.g. "hindi", "arabic", "english"
        source_script:   e.g. "latin", "devanagari", "arabic", "chinese"

    If already English, returns (text, "english", "latin").
    On any failure, returns (text, "english", "latin") so the pipeline continues safely.

    Script matters because users may write a language in a non-native script
    (e.g. Hindi written in Latin/Roman letters — Hinglish). The response
    must match both the language AND the script the user chose.
    """
    system_prompt = (
        "Detect the language and writing script of the user's message, then translate it to English.\n"
        "Respond with a JSON object only — no explanation, no markdown.\n\n"
        "Format:\n"
        "{\"language\": \"<language name in lowercase>\", "
        "\"script\": \"<script name in lowercase>\", "
        "\"english\": \"<english translation>\"}\n\n"
        "CRITICAL — HOW TO DETECT SCRIPT:\n"
        "Look ONLY at the actual characters present in the text. Do NOT infer script from language.\n"
        "- If the text uses A-Z letters (Latin/Roman alphabet) → script is 'latin', "
        "regardless of what language it is.\n"
        "  Examples of Latin-script messages: 'chutti kaise le' (Hindi in Latin), "
        "'shukriya' (Arabic/Urdu in Latin), 'merci' (French), 'danke' (German)\n"
        "- If the text uses Devanagari characters (Hindi/Marathi native script) → 'devanagari'\n"
        "- If the text uses Arabic letters → 'arabic'\n"
        "- If the text uses Chinese characters → 'chinese'\n"
        "- Apply the same logic for cyrillic, japanese, korean, gujarati, tamil, telugu, bengali\n\n"
        "For script, use one of: latin, devanagari, arabic, chinese, cyrillic, "
        "japanese, korean, gujarati, tamil, telugu, bengali, or other.\n\n"
        "Examples:\n"
        "{\"language\": \"english\", \"script\": \"latin\", \"english\": \"What is the MOQ for silk?\"}\n"
        "{\"language\": \"hindi\", \"script\": \"latin\", \"english\": \"How do I take leave?\"}\n"
        "{\"language\": \"hindi\", \"script\": \"devanagari\", \"english\": \"How do I take leave?\"}\n"
        "{\"language\": \"arabic\", \"script\": \"latin\", \"english\": \"What are your payment terms?\"}\n"
        "{\"language\": \"arabic\", \"script\": \"arabic\", \"english\": \"What are your payment terms?\"}"
    )

    try:
        completion = _get_client().chat.completions.create(
            model=TRANSLATOR_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": text},
            ],
            max_tokens=300,
            temperature=0,
        )
        raw   = completion.choices[0].message.content.strip()
        clean = re.sub(r'```json\s*|\s*```', '', raw).strip()
        data  = json.loads(clean)

        language     = data.get("language", "english").lower().strip()
        script       = data.get("script",   "latin").lower().strip()
        english_text = data.get("english",  text).strip()

        if language != "english":
            log.info(
                f"  [translator] detected='{language}' script='{script}' | "
                f"'{text[:60]}' → '{english_text[:60]}'"
            )
        else:
            log.info(f"  [translator] detected='english' — no translation needed")

        return english_text, language, script

    except Exception as e:
        log.warning(f"  [translator] detect_and_translate failed ({e}) — using original")
        return text, "english", "latin"


def translate_to_language(english_text: str, target_language: str, target_script: str) -> str:
    """
    Translate English text to the target language and script.
    Returns text unchanged if target_language is "english".

    When target_script is "latin" and the target language is not English,
    the model is explicitly told to romanize (use Latin letters only, no native script).
    This handles cases like Hinglish, romanized Arabic, etc.
    """
    if target_language.lower() == "english":
        return english_text

    if target_script == "latin":
        instruction = (
            f"Translate the following English text to {target_language}.\n"
            f"IMPORTANT: Write your response using the Latin/Roman alphabet only "
            f"(romanized {target_language}). "
            f"Do NOT use any native script such as Devanagari, Arabic letters, "
            f"Chinese characters, or any other non-Latin writing system.\n"
            f"Output only the translation, nothing else."
        )
    else:
        instruction = (
            f"Translate the following English text to {target_language} "
            f"using {target_script} script.\n"
            f"Output only the translation, nothing else."
        )

    try:
        completion = _get_client().chat.completions.create(
            model=TRANSLATOR_MODEL_NAME,
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user",   "content": english_text},
            ],
            max_tokens=500,
            temperature=0,
        )
        translated = completion.choices[0].message.content.strip()
        log.info(f"  [translator] → {target_language} ({target_script}): '{translated[:80]}'")
        return translated

    except Exception as e:
        log.warning(f"  [translator] translate_to_language failed ({e}) — returning English")
        return english_text