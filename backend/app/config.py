# backend/app/config.py
from dotenv import load_dotenv
import os

load_dotenv()

os.environ["ANONYMIZED_TELEMETRY"] = "False"

# ── Core ──────────────────────────────────────────────────────────────────────
GROQ_API_KEY       = os.getenv("GROQ_API_KEY")
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_store")
DATA_DIR           = os.getenv("DATA_DIR", "./data")
TOP_K = int(os.getenv("TOP_K", 8))
COLLECTION_NAME    = "sop_docs"
MODEL_NAME         = "llama-3.3-70b-versatile"
EMBED_MODEL_NAME   = "sentence-transformers/all-MiniLM-L6-v2"

# Shared between ingest.py and kb_writer.py - must stay in sync
CHUNK_SIZE         = int(os.getenv("CHUNK_SIZE", 256))
CHUNK_OVERLAP      = int(os.getenv("CHUNK_OVERLAP", 32))

# ── General LLM ───────────────────────────────────────────────────────────────
# Simplified — just answers the user, no lifecycle responsibility
#not necessarily friendly. keep it neutral and helpful
GENERAL_LLM_SYSTEM_PROMPT = os.getenv(
    "GENERAL_LLM_SYSTEM_PROMPT",
    (
        "You are a helpful and friendly assistant for a B2B export company. "
        "Respond to the user's message directly and concisely.\n\n"
        "- Question → answer it clearly.\n"
        "- Greeting (Hi, Hello, Hey, Good morning) → respond warmly. "
        "Example: 'Hello!' or 'Hi there!'\n"
        "- Gratitude (Thank you, Thanks, Much appreciated, Cheers) → "
        "respond warmly. Example: 'You're welcome!' or 'Happy to help!'\n"
        "- Conversation ender (No thanks, That's all, No, Bye, I'm good, "
        "No need, We're done, No further questions) → respond with a warm, "
        "friendly closing. Never say just 'Goodbye.' "
        "Examples: 'Have a wonderful day!', 'It was a pleasure assisting you!', "
        "'Take care, and feel free to reach out anytime!', "
        "'Glad I could help — wishing you a great day ahead!'\n\n"
        "Output only your response. Nothing else."
    ),
)
GENERAL_LLM_MAX_TOKENS = int(os.getenv("GENERAL_LLM_MAX_TOKENS", 512))

# Separate model for lifecycle — cheap, fast, single-purpose
LIFECYCLE_MODEL_NAME = os.getenv("LIFECYCLE_MODEL_NAME", "llama-3.1-8b-instant")

LIFECYCLE_SYSTEM_PROMPT = os.getenv(
    "LIFECYCLE_SYSTEM_PROMPT",
    (
        "You manage the follow-up question in a customer service chat.\n\n"
        "Given the conversation history and the user's latest message, "
        "output exactly ONE follow-up question OR the single word NONE.\n\n"
        "Rules — apply in order, stop at first match:\n"
        "1. If the user's message signals the end of the conversation "
        "(e.g. No thanks, That's all, I'm good, Bye, No need, We're done, "
        "No further questions, or any equivalent in any language) → output: NONE\n"
        "2. If the user's message is a simple affirmative (Yes, Yeah, Yep, Sure) "
        "in response to a continuation question like 'Is there anything else I can "
        "help you with?' → output an inviting question.\n"
        "   Vary between: 'What would you like help with?', "
        "'Of course! What\\'s on your mind?', 'Go ahead, I\\'m here to help!'\n"
        "3. If conversation history is empty → output an opening question.\n"
        "   Vary between: 'How can I be of assistance?', "
        "'What can I help you with today?', 'Is there something I can help you with?'\n"
        "4. If the last assistant message in history was a closing/goodbye → "
        "output an opening question (same options as Rule 3).\n"
        "5. Otherwise → output a continuation question.\n"
        "   Vary between: 'Is there anything else I can help you with?', "
        "'Do you require further assistance with anything?', "
        "'What else can I help you with?'\n\n"
        "Output ONLY the question or the word NONE. No explanation. No preamble."
    ),
)


#try to humanize more
# ── LLM Judge ─────────────────────────────────────────────────────────────────
JUDGE_MODEL_NAME = os.getenv("JUDGE_MODEL_NAME", "llama-3.3-70b-versatile")

JUDGE_SYSTEM_PROMPT = os.getenv(
    "JUDGE_SYSTEM_PROMPT",
    (
        "You are evaluating whether a set of retrieved text chunks contains "
        "enough information to answer a query.\n"
        "\n"
        "You must NOT answer the query yourself.\n"
        "You must NOT assume information that is not explicitly present in the chunks.\n"
        "\n"
        "Reply with exactly one word:\n"
        "'sufficient'   - the chunks are topically relevant and likely contain "
        "enough information to answer the query.\n"
        "'insufficient' - the chunks are clearly off-topic or contain no information "
        "related to the query.\n"
        "\n"
        "When in doubt, reply 'sufficient' - it is better to attempt an answer "
        "from the knowledge base than to escalate unnecessarily."
    ),
)

#keep insufficient when doubt - escalations will slowly make our kb stronger with time

# ── Query Rewriter ────────────────────────────────────────────────────────────
REWRITER_MODEL_NAME = os.getenv("REWRITER_MODEL_NAME", "llama-3.3-70b-versatile")

REWRITER_SYSTEM_PROMPT = os.getenv(
    "REWRITER_SYSTEM_PROMPT",
    (
        "You are a query rewriter for a conversational assistant.\n"
        "Your job is to rewrite the user's latest message into a complete, "
        "self-contained question or statement that makes sense without "
        "reading the conversation history.\n"
        "\n"
        "Rules — follow in order, stop at the first match:\n"
        "1. If the message is a conversational acknowledgment, filler, or reaction "
        "(e.g. 'Ok', 'Okay', 'Thanks', 'Alright', 'Got it', 'Sure', 'I see', "
        "'Hmm', 'Fine', 'Great', 'Nice', 'Cool', 'Yes', 'No', 'Yeah', 'Nah', "
        "'Yep', 'Nope', or any equivalent in any language), "
        "return it UNCHANGED. Do NOT infer or construct a question from prior context.\n"
        "2. If the message is a greeting (e.g. 'Hi', 'Hello', 'Hey', 'Good morning'), "
        "return it UNCHANGED.\n"
        "3. If the message is a self-introduction or personal statement about the user "
        "(e.g. 'I am Rishabh', 'My name is X', 'I work at Y', 'I am a manager'), "
        "return it UNCHANGED. Do NOT convert it into a question.\n"
        "4. If the message is a complete, self-contained question about the assistant, "
        "the user themselves, or general knowledge that requires no business context "
        "(e.g. 'Who are you', 'What is my name', 'What is the capital of France'), "
        "return it UNCHANGED.\n"
        "5. If a 'The user is directly replying to this specific message' section is "
        "present, treat that quoted message as the PRIMARY context for understanding "
        "what the user is referring to. Use the conversation history only as secondary "
        "context. Rewrite the message to make it fully self-contained based on what "
        "the user is replying to.\n"
        "6. If the message is already a complete, self-contained question or request "
        "that can be understood without any context, return it UNCHANGED.\n"
        "7. ONLY if the message is a question or request that is clearly missing context "
        "(uses 'it', 'that', 'those', 'the same', or refers to a subject mentioned "
        "earlier without naming it) — rewrite it using the conversation history to "
        "make it self-contained. Add ONLY the missing subject. Do NOT add any other "
        "details from the conversation history that were not referenced in the message.\n"
        "\n"
        "Additional constraints:\n"
        "- Do NOT add information that is not present in the conversation.\n"
        "- Do NOT answer the question. Only rewrite it.\n"
        "- Output ONLY the rewritten query. No explanation, no preamble.\n"
        "- Always output in English."
    ),
)

# ── LLM Router ────────────────────────────────────────────────────────────────
ROUTER_MODEL_NAME = os.getenv("ROUTER_MODEL_NAME", "llama-3.3-70b-versatile")

ROUTER_SYSTEM_PROMPT = os.getenv(
    "ROUTER_SYSTEM_PROMPT",
    (
        "You are a fallback classifier for a company assistant.\n"
        "A query has already been checked against the company knowledge base "
        "and could not be answered from it.\n"
        "Your job is to classify why:\n"
        "\n"
        "'company': the query is business-related - about products, pricing, orders, "
        "shipping, complaints, payment terms, export procedures, supplier queries, "
        "or any topic the company should know about. These will be escalated to a human.\n"
        "'general': the query is general knowledge, small talk, greetings, or "
        "entirely unrelated to the company's business.\n"
        "\n"
        "When in doubt, reply 'company' - it is safer to escalate than to ignore "
        "a potentially important business query.\n"
        "\n"
        "Reply with exactly one word: company or general."
    ),
)

# ── Escalation ────────────────────────────────────────────────────────────────
ESCALATION_JID             = os.getenv("ESCALATION_JID", "")
ESCALATION_DB_PATH         = os.getenv("ESCALATION_DB_PATH", "./data/escalations.db")
ESCALATION_LOG_PATH        = os.getenv("ESCALATION_LOG_PATH", "./data/escalations.txt")
ESCALATION_HOLDING_MESSAGE = os.getenv(
    "ESCALATION_HOLDING_MESSAGE",
    #"I don't have enough information to answer that accurately. "
    "Let me check with the team. Pls give me some time. ",
)

# ── Translator ────────────────────────────────────────────────────────────────
# Uses 70B for quality — translation errors are visible to the user.
TRANSLATOR_MODEL_NAME = os.getenv("TRANSLATOR_MODEL_NAME", "llama-3.3-70b-versatile")