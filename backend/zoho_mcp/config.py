# backend/zoho_mcp/config.py
"""
Phase 0 configuration for the standalone Zoho MCP integration.

This module is deliberately independent of app.config so the Phase 0 work can be
validated in isolation, with zero risk to the live RAG pipeline. It reuses only
GROQ_API_KEY / MODEL_NAME from the same .env for convenience.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Zoho MCP server ───────────────────────────────────────────────────────────
# The server URL generated in the Zoho MCP console. Include the full path
# (typically ending in /mcp). NOTE the trailing-slash gotcha — see README.
ZOHO_MCP_URL = os.getenv("ZOHO_MCP_URL", "")

# Simplest auth path: a bearer token sent in the Authorization header.
# Confirm the exact mechanism in the Zoho MCP console when you create the server.
# If Zoho gives you an interactive OAuth flow instead, see build_auth_headers()
# and the README's "Authentication" section.
ZOHO_MCP_AUTH_TOKEN = os.getenv("ZOHO_MCP_AUTH_TOKEN", "")

# Any extra headers the server needs, formatted as "Key1:Value1;Key2:Value2".
ZOHO_MCP_EXTRA_HEADERS = os.getenv("ZOHO_MCP_EXTRA_HEADERS", "")

MCP_TIMEOUT = float(os.getenv("ZOHO_MCP_TIMEOUT", "30"))

# ── LLM (reused from the main app so the eval reflects production behaviour) ────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
AGENT_MODEL = os.getenv("ZOHO_AGENT_MODEL", os.getenv("MODEL_NAME", "llama-3.3-70b-versatile"))

# ── Cost estimate (optional) ────────────────────────────────────────────────
# Set these to your current Groq per-1M-token rate to get a cost column in the
# eval report. Leave at 0 to skip cost reporting.
PRICE_PER_MTOK_INPUT = float(os.getenv("PRICE_PER_MTOK_INPUT", "0"))
PRICE_PER_MTOK_OUTPUT = float(os.getenv("PRICE_PER_MTOK_OUTPUT", "0"))


def build_auth_headers() -> dict:
    """
    Build the HTTP headers passed to the MCP transport.

    Default: a single Authorization: Bearer <token> header. This is the seam to
    extend when you wire Zoho's OAuth refresh-token flow for unattended renewal —
    fetch a fresh access token here and return it in the header.
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
