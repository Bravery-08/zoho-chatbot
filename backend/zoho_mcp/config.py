# backend/zoho_mcp/config.py
"""
Configuration for the Zoho MCP integration (Phase 0 and Phase 1).

Deliberately independent of app.config so the zoho_mcp package can be
validated in isolation. All values come from the same backend/.env file.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Zoho MCP server ───────────────────────────────────────────────────────────
ZOHO_MCP_URL         = os.getenv("ZOHO_MCP_URL", "")
ZOHO_MCP_AUTH_TOKEN  = os.getenv("ZOHO_MCP_AUTH_TOKEN", "")
ZOHO_MCP_EXTRA_HEADERS = os.getenv("ZOHO_MCP_EXTRA_HEADERS", "")
MCP_TIMEOUT          = float(os.getenv("ZOHO_MCP_TIMEOUT", "30"))

# ── Zoho organisation ─────────────────────────────────────────────────────────
# Found in Zoho Books → Settings → Organisation Profile.
# Injected automatically into every tool call so the model never has to guess it.
ZOHO_ORG_ID = os.getenv("ZOHO_ORG_ID", "")

# ── LLM ──────────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# Strong model: tool selection + synthesis (matches app MODEL_NAME)
AGENT_MODEL  = os.getenv("ZOHO_AGENT_MODEL",
               os.getenv("MODEL_NAME", "llama-3.3-70b-versatile"))

# Fast model: intent classification (matches app LIFECYCLE_MODEL_NAME)
INTENT_MODEL = os.getenv("ZOHO_INTENT_MODEL",
               os.getenv("LIFECYCLE_MODEL_NAME", "llama-3.1-8b-instant"))

# ── Tool schema cache ─────────────────────────────────────────────────────────
# Seconds before the agent re-fetches tool schemas from the MCP server.
# First call after startup (or after TTL) takes ~500 ms for the MCP round-trip;
# subsequent calls within the TTL window are instant.
TOOL_CACHE_TTL = int(os.getenv("ZOHO_TOOL_CACHE_TTL", "300"))

# ── Eval cost reporting (optional) ───────────────────────────────────────────
PRICE_PER_MTOK_INPUT  = float(os.getenv("PRICE_PER_MTOK_INPUT",  "0"))
PRICE_PER_MTOK_OUTPUT = float(os.getenv("PRICE_PER_MTOK_OUTPUT", "0"))


def build_auth_headers() -> dict:
    """
    HTTP headers passed to the MCP transport.
    Default: Authorization: Bearer <token>.
    For unattended OAuth renewal, fetch a fresh access token here and return it.
    """
    headers: dict[str, str] = {}
    if ZOHO_MCP_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {ZOHO_MCP_AUTH_TOKEN}"
    if ZOHO_MCP_EXTRA_HEADERS:
        for pair in ZOHO_MCP_EXTRA_HEADERS.split(";"):
            if ":" in pair:
                k, v = pair.split(":", 1)
                headers[k.strip()] = v.strip()
    return headers