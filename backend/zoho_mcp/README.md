# Phase 0 — Zoho MCP bedrock

Standalone validation of the Zoho One integration **before any of it touches the
WhatsApp pipeline**. Nothing in this folder imports from `app/`, and `app/`
doesn't import from here. You can delete the whole `zoho_mcp/` folder and the bot
keeps running unchanged.

```
backend/zoho_mcp/
├── __init__.py
├── config.py            # env: server URL, auth, model, cost rates
├── client.py            # MCP streamable-HTTP session + Groq schema converter
├── smoke_test.py        # connect · list/dump tools · call one tool by hand
├── run_eval.py          # corpus → LLM(tools) → tool-selection + arg scoring
├── corpus.jsonl         # the command corpus (starter; expand to 40)
├── .env.example         # the new env vars
├── requirements-zoho.txt
└── eval_results/        # schema dump + timestamped eval reports (gitignore this)
```

## What this proves (the exit gate)

> ≥95% tool-selection accuracy and correct args on the read-only subset of the
> corpus, with a written report of which tools are reliable, which are flaky, and
> p50/p95 latency.

Do not start Phase 1 (wiring the agent to WhatsApp) until this passes.

---

## Setup

**1. Stand up the server (Zoho MCP console, IN data center).**
Start with the **read-only** server only — e.g. for CRM, the Data Insights
server that can query records without touching them. Do **not** enable the
write / operations / payments servers yet.

**2. Create a dedicated, narrowly-scoped Zoho service user for the agent.**
Zoho MCP inherits the connected user's role and nothing beyond it, so this is
where least-privilege is enforced — at Zoho, not in a prompt. The agent should
authenticate as this user, never as you.

**3. Dependencies** (Python ≥ 3.10):

```bash
# from backend/
pip install -r zoho_mcp/requirements-zoho.txt
# or add `mcp>=1.10,<2` to backend/requirements.txt and reinstall
```

**4. Env** — copy the vars from `zoho_mcp/.env.example` into `backend/.env` and
fill in `ZOHO_MCP_URL` and the auth token.

**5. Gitignore the outputs:**

```bash
echo "backend/zoho_mcp/eval_results/" >> .gitignore
```

---

## Run order

```bash
# from backend/  (so `zoho_mcp` is importable, same as how app.* is run)

# 1. Connect and see the real tools. Writes eval_results/tools_schema.json.
python -m zoho_mcp.smoke_test list

# 2. Sanity-check a couple of READ tools by hand (use real names + args).
python -m zoho_mcp.smoke_test call zohobooks__list_invoices '{"status": "unpaid"}'

# 3. Edit corpus.jsonl: set every expected_tool to a real name; expand to 40.

# 4. Run the eval (fetches the live tool list, scores selection + args).
python -m zoho_mcp.run_eval

#    …or score offline against the dumped schema (no Zoho creds needed, CI-able):
python -m zoho_mcp.run_eval --schema-file eval_results/tools_schema.json
```

---

## Authentication

The simplest path, wired by default: a **bearer token** in the `Authorization`
header (`ZOHO_MCP_AUTH_TOKEN`). Use this to get moving.

Confirm the exact mechanism when you create the server in the console. If Zoho
hands you an **interactive OAuth** flow instead of a paste-able token, the MCP
SDK supports it via `mcp.client.auth.OAuthClientProvider` (passed as `auth=` to
`streamablehttp_client`). For unattended/headless renewal, the clean approach is
a Zoho **self-client → refresh token → access token** exchange done inside
`config.build_auth_headers()`, so every connection gets a fresh token. Leave
that until you actually need it; the bearer header is enough for Phase 0.

---

## How scoring works

`run_eval.py` asks your Groq model to pick a tool + args for each command, given
the real MCP tool schemas, then compares against the corpus. It **does not call
any Zoho tool** — selection scoring needs the tool *schema*, not live data, which
keeps runs fast, deterministic, and safe. (Use `smoke_test.py call` to actually
execute a read tool.)

- **tool-selection accuracy** — first tool call matches `expected_tool`
  (string, list of acceptable names, or `null` = expect no call).
- **arg accuracy** — per-key subset match over `expected_args`, scored only on
  rows where the tool was correct.
- **latency / tokens / cost** — wall-clock per call; tokens from Groq usage;
  cost only if you set the `PRICE_PER_MTOK_*` rates.

Every run writes a full JSON report to `eval_results/eval-<timestamp>.json` and
prints a summary plus a list of misses. The corpus is your regression suite from
here on — when Phase 1+ surfaces a real-world failure, add it as a new row.

---

## Common gotchas

- **Trailing slash.** Streamable-HTTP servers are usually mounted at `/mcp`; a
  missing/extra trailing slash can trigger a `307` redirect that breaks the POST.
  If `initialize()` hangs or errors, try toggling the slash on `ZOHO_MCP_URL`.
- **Wrong data center.** You're in IN — make sure the server URL is the `.in`
  endpoint, not `.com`.
- **Corpus tool names drift.** If you re-create the server and tool names change,
  `run_eval` warns about any `expected_tool` that no longer exists. Re-dump with
  `smoke_test list` and update the corpus.
- **Over-calling on negatives.** If the model calls a tool on the `null` rows
  (weather, "thanks"), that's a real signal — tighten the system prompt in
  `run_eval.AGENT_SYSTEM_PROMPT` (it's meant to mirror the eventual agent prompt).
