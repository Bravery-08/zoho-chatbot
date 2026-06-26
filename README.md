# WhatsApp × Zoho One — AI-Native Enterprise Control Plane

A production system that turns WhatsApp into the primary user interface for a B2B
merchant-export company, with Zoho One as the enterprise data layer and AI agents
as the operational workforce.

**Humans only touch:** exceptions, approvals, negotiations, and strategy.  
**No employee:** re-enters data, forwards messages, or prepares reports.

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [How a Message Flows](#2-how-a-message-flows)
3. [Project Structure](#3-project-structure)
4. [Prerequisites](#4-prerequisites)
5. [Setup Guide](#5-setup-guide)
6. [Environment Variables Reference](#6-environment-variables-reference)
7. [Running the System](#7-running-the-system)
8. [Feature Reference](#8-feature-reference)
9. [API Endpoints](#9-api-endpoints)
10. [The Approval Flow](#10-the-approval-flow)
11. [The Workflow State Machine](#11-the-workflow-state-machine)
12. [The Learning Loop](#12-the-learning-loop)
13. [Eval and Regression Suite](#13-eval-and-regression-suite)
14. [Operations and Monitoring](#14-operations-and-monitoring)
15. [Troubleshooting](#15-troubleshooting)
16. [Phase Build History](#16-phase-build-history)

---

## 1. System Architecture

```
WhatsApp (Baileys bot / Official Cloud API)
        │
        ▼
  FastAPI backend (app/main.py)
        │
        ├── Identity resolution    (zoho_mcp/identity.py)
        │     JID → internal | known customer | unknown
        │
        ├── Intent classification  (zoho_mcp/intent.py)
        │     6-way: answer_from_kb | read_zoho | write_zoho
        │            ops_query | general | escalate
        │
        ├── RAG pipeline           (app/rag.py, judge.py, rewriter.py)
        │     ChromaDB + sentence-transformers + Groq
        │
        ├── Read agent             (zoho_mcp/agent.py)
        │     Groq + Zoho MCP read tools → live data
        │
        ├── Write agent            (zoho_mcp/write_agent.py)
        │     Proposal → confirm → execute (low-risk)
        │     Proposal → confirm → approve → execute (high-risk)
        │
        ├── Workflow engine        (zoho_mcp/workflow.py)
        │     Multi-step: estimate → SO (Quote-to-Order)
        │
        ├── Learning loop          (zoho_mcp/learning.py)
        │     Feedback → trust → graduation → autonomy
        │
        └── Ops visibility         (zoho_mcp/ops.py, digest.py)
              Daily digest + alerts → WhatsApp outbox

        │
        ▼
  Zoho One (via Zoho MCP servers)
        ├── Read server  — 11 tools (CRM, Books, Inventory)
        └── Write server — 3 tools (restricted service user)
```

---

## 2. How a Message Flows

```
Customer sends WhatsApp message
        │
        ▼
1. Translate to English          (app/translator.py)
2. Rewrite with history context  (app/rewriter.py)
3. Resolve sender identity       (zoho_mcp/identity.py)
        ├── internal   → full tool access (staff JID)
        ├── known      → scoped to their account (CRM match)
        └── unknown    → KB/general only, no Zoho data
4. Check pending confirmation    (zoho_mcp/confirm.py)
        └── "yes/no" → execute or cancel pending action
5. Check active workflow         (zoho_mcp/workflow.py)
        └── "accept/status/cancel" → workflow-aware response
6. Classify intent               (zoho_mcp/intent.py)
        ├── answer_from_kb  → RAG pipeline
        ├── read_zoho       → Read agent (Zoho MCP)
        ├── write_zoho      → Write agent (propose → confirm)
        ├── ops_query       → Ops agent (local SQLite, internal only)
        ├── general         → General LLM
        └── escalate        → Human notification
7. Translate response back       (app/translator.py)
8. Return to WhatsApp
```

---

## 3. Project Structure

```
zoho-chatbot/
├── backend/
│   ├── app/                        # Original RAG pipeline (unchanged)
│   │   ├── main.py                 # FastAPI app — extended in each phase
│   │   ├── config.py               # App-level config
│   │   ├── rag.py                  # ChromaDB retrieval + synthesis
│   │   ├── judge.py                # Chunk sufficiency judge
│   │   ├── rewriter.py             # Query rewriter
│   │   ├── router.py               # Legacy binary classifier (superseded)
│   │   ├── escalate.py             # Human escalation queue
│   │   ├── kb_writer.py            # Live ChromaDB write-back
│   │   ├── translator.py           # Multilingual support
│   │   └── general.py              # General LLM answers
│   │
│   ├── zoho_mcp/                   # Zoho integration (Phases 0–6)
│   │   ├── config.py               # MCP URLs, org ID, model names, thresholds
│   │   ├── client.py               # Streamable-HTTP MCP session + schema sanitizer
│   │   ├── identity.py             # JID → Zoho identity (Phase 2)
│   │   ├── scope.py                # Tool allowlist + customer scoping (Phase 2)
│   │   ├── intent.py               # 6-way intent classifier (Phases 1, 3, 5)
│   │   ├── agent.py                # Read agent loop (Phase 1)
│   │   ├── write_agent.py          # Write proposals + execution (Phase 3)
│   │   ├── confirm.py              # Pending action store (Phase 3)
│   │   ├── audit.py                # Append-only write audit log (Phase 3)
│   │   ├── workflow.py             # Multi-step workflow state machine (Phase 4)
│   │   ├── ops.py                  # Operational metrics from local SQLite (Phase 5)
│   │   ├── digest.py               # Daily digest + outbox (Phase 5)
│   │   ├── learning.py             # Feedback store + graduated autonomy (Phase 6)
│   │   ├── smoke_test.py           # MCP connection + tool smoke test
│   │   ├── run_eval.py             # Tool-selection eval harness
│   │   └── corpus.jsonl            # 40-command regression corpus
│   │
│   ├── data/
│   │   ├── sops.txt                # Company SOPs and product knowledge
│   │   ├── escalations.db          # Escalation queue (SQLite)
│   │   └── write_actions.db        # Confirms, audit, workflows, outbox, learning
│   │
│   └── requirements.txt
│
└── bot/
    └── src/
        ├── index.js                # Baileys connection + outbox polling
        ├── handler.js              # Message routing + escalation delivery
        └── api.js                  # FastAPI client functions
```

---

## 4. Prerequisites

- **Python** ≥ 3.10
- **Node.js** ≥ 18
- **Zoho One** account (trial sufficient)
- **Groq API key** (free tier works for development)
- A **WhatsApp number** for the bot (personal number is fine for dev)
- A second WhatsApp number as the **escalation / operator number**

---

## 5. Setup Guide

### 5.1 Backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
pip install "mcp>=1.10,<2" "httpx<0.28.0"
```

### 5.2 Bot

```bash
cd bot
npm install
```

### 5.3 Zoho MCP — Read Server

1. Go to [mcp.zoho.in](https://mcp.zoho.in) (IN data centre)
2. Create server → **"WhatsApp Agent — Read"**
3. Connection → **Authorization via Connection** → authorize each service:
   - Zoho CRM, Zoho Books, Zoho Inventory
4. Tools → Add Tools → add these (read/list/get only):
   - `ZohoCRM_getRecord`, `ZohoCRM_searchRecords`
   - `ZohoBooks_list_invoices`, `ZohoBooks_list_estimates`
   - `ZohoBooks_list_sales_orders`, `ZohoBooks_get_customer_balances_report`
   - `ZohoBooks_list_contacts`
   - `ZohoInventory_get_item`, `ZohoInventory_get_sales_order`
   - `ZohoInventory_list_purchase_orders`, `ZohoInventory_get_shipment_order`
5. Connect tab → copy the **MCP URL** (contains the API key)

Verify with:
```bash
python -m zoho_mcp.smoke_test list
```

### 5.4 Zoho MCP — Write Server

1. **Create a restricted Zoho One user** (Zoho One → Directory → Add User)
   - Zoho CRM role: View + Create Leads only (no delete)
   - Zoho Books role: View + Create Estimates + Create Sales Orders (no delete/send)
2. Sign into [mcp.zoho.in](https://mcp.zoho.in) **as the restricted user**
3. Create server → **"WhatsApp Agent — Write (Restricted)"**
4. Connection → Authorization via Connection → authorize CRM + Books as restricted user
5. Tools → Add Tools:
   - `ZohoCRM_createRecords`
   - `ZohoBooks_create_estimate`
   - `ZohoBooks_create_sales_order`
6. Connect tab → copy the **Write MCP URL**

Verify with:
```bash
$env:ZOHO_MCP_URL = "<write-server-url>"
python -m zoho_mcp.smoke_test list
```

### 5.5 Knowledge Base

Add company content to `backend/data/sops.txt`:
- Payment terms, shipping procedures, MOQs
- Product catalogue with specifications
- Complaint handling policies
- Frequently asked questions

Then ingest:
```bash
cd backend
python ingest.py
```

### 5.6 Environment Variables

Copy `backend/zoho_mcp/.env.example` to `backend/.env` and fill in all values.
See [Section 6](#6-environment-variables-reference) for the full reference.

---

## 6. Environment Variables Reference

All variables go in `backend/.env`.

### Zoho MCP

| Variable | Required | Description |
|---|---|---|
| `ZOHO_MCP_URL` | ✅ | Read server URL from the MCP console Connect tab |
| `ZOHO_WRITE_MCP_URL` | ✅ | Write server URL (restricted user) |
| `ZOHO_MCP_AUTH_TOKEN` | — | Bearer token if server uses header auth (usually empty — key is in URL) |
| `ZOHO_ORG_ID` | ✅ | Zoho Books org ID: Settings → Organisation Profile |
| `ZOHO_MCP_TIMEOUT` | — | MCP connection timeout in seconds (default: 30) |
| `ZOHO_TOOL_CACHE_TTL` | — | Seconds before re-fetching tool schemas (default: 300) |

### LLM

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | ✅ | Groq API key |
| `MODEL_NAME` | — | Strong model for agent/synthesis (default: llama-3.3-70b-versatile) |
| `LIFECYCLE_MODEL_NAME` | — | Fast model for routing/intent (default: llama-3.1-8b-instant) |
| `ZOHO_AGENT_MODEL` | — | Override model for Zoho agent specifically |
| `ZOHO_INTENT_MODEL` | — | Override model for intent classifier specifically |

### Identity

| Variable | Required | Description |
|---|---|---|
| `STAFF_JIDS` | ✅ | Comma-separated staff phone numbers with country code, no `+` (e.g. `917977909705`) |
| `IDENTITY_CACHE_TTL` | — | Seconds to cache CRM identity lookups (default: 3600) |

### Write Actions

| Variable | Required | Description |
|---|---|---|
| `WRITE_ACTIONS_DB_PATH` | — | SQLite path for all write state (default: ./data/write_actions.db) |
| `CONFIRMATION_TIMEOUT` | — | Seconds before pending confirmation expires (default: 600) |
| `WORKFLOW_TTL` | — | Seconds before active workflow expires (default: 86400) |

### Learning / Graduation

| Variable | Required | Description |
|---|---|---|
| `GRADUATION_THRESHOLD` | — | Consecutive clean approvals needed to promote a tool (default: 5) |
| `DEGRADE_THRESHOLD` | — | Consecutive rejections to demote a graduated tool (default: 2) |

### Ops / Digest

| Variable | Required | Description |
|---|---|---|
| `DIGEST_HOUR` | — | Hour (24h) to send the daily digest (default: 9) |
| `APPROVAL_ALERT_HOURS` | — | Alert if approval pending longer than this (default: 4) |
| `WORKFLOW_STALE_HOURS` | — | Alert if estimate unaccepted longer than this (default: 24) |

### Eval

| Variable | Required | Description |
|---|---|---|
| `PRICE_PER_MTOK_INPUT` | — | Groq input token cost per 1M for eval cost reporting |
| `PRICE_PER_MTOK_OUTPUT` | — | Groq output token cost per 1M for eval cost reporting |

### Existing App Variables

| Variable | Required | Description |
|---|---|---|
| `ESCALATION_JID` | ✅ | Operator WhatsApp JID for approvals and escalations |
| `ESCALATION_HOLDING_MESSAGE` | — | Message sent to customer while waiting for human |

---

## 7. Running the System

### Backend (FastAPI)

```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

Startup log confirms readiness:
```
RAG pipeline ready ✅
Escalation DB ready ✅
Write action DB ready ✅
```

### Bot (Baileys)

```bash
cd bot
node src/index.js
```

First run shows a QR code — scan with WhatsApp on your phone.

### Testing without WhatsApp

```powershell
# Any query
$body = @{ message="show my invoices"; sender="91YOUR_NUMBER@s.whatsapp.net"; history=@() } | ConvertTo-Json
Invoke-RestMethod -Uri http://localhost:8000/query -Method Post -ContentType "application/json" -Body $body

# Trigger digest manually
Invoke-RestMethod -Uri http://localhost:8000/digest/now -Method Post

# Check ops summary
Invoke-RestMethod -Uri http://localhost:8000/ops/summary
```

---

## 8. Feature Reference

### 8.1 Knowledge Base (RAG)

Answers questions about company policy, SOPs, product catalogue, payment terms,
and shipping procedures from ChromaDB.

**Triggered by:** `answer_from_kb` intent  
**Data source:** `data/sops.txt` (ingested into ChromaDB)  
**Behaviour:** Retrieves relevant chunks → judge checks sufficiency → synthesizes answer  
**Fallback:** Insufficient chunks → escalates to operator

**Example queries:**
- "What are your payment terms?"
- "What's the MOQ for cotton?"
- "How do you handle complaints?"

**To update the KB:**
```bash
# Edit backend/data/sops.txt, then:
python ingest.py
```

### 8.2 Live Zoho Data Queries

Answers questions about live business data from Zoho CRM, Books, and Inventory.

**Triggered by:** `read_zoho` intent  
**Data source:** Zoho MCP read server (11 tools)  
**Scoping:** Customers only see their own records (enforced in code, not prompts)

**Customer-accessible tools:**
| Tool | What it returns |
|---|---|
| `ZohoBooks_list_invoices` | Customer's own invoices (filtered by account) |
| `ZohoBooks_list_estimates` | Customer's own estimates |
| `ZohoBooks_list_sales_orders` | Customer's own sales orders |
| `ZohoInventory_get_sales_order` | Specific SO (ownership verified) |
| `ZohoInventory_get_shipment_order` | Specific shipment (ownership verified) |

**Internal (staff) tools — additional:**
| Tool | What it returns |
|---|---|
| `ZohoCRM_searchRecords` | Any CRM records |
| `ZohoCRM_getRecord` | Any record by ID |
| `ZohoInventory_get_item` | Stock levels |
| `ZohoInventory_list_purchase_orders` | All POs |
| `ZohoBooks_get_customer_balances_report` | All customer balances |
| `ZohoBooks_list_contacts` | All Books contacts |

**Example queries:**
- "Show my unpaid invoices"
- "What's the status of SO-00142?"
- "How much do I owe?"

### 8.3 Write Actions

Creates or updates Zoho records via a two-step confirm-then-execute flow.

**Triggered by:** `write_zoho` intent  
**Data source:** Zoho MCP write server (restricted user, 3 tools)

#### Risk Tiers

| Risk | Tools | Execution |
|---|---|---|
| Low | `ZohoCRM_createRecords`, `ZohoBooks_create_estimate` | Customer confirms → executes immediately |
| High | `ZohoBooks_create_sales_order` | Customer confirms → operator approves → executes |

#### Confirm-Then-Execute Flow

```
Customer: "I need a quote for 200 bags of basmati rice at ₹2800"
Bot:      "I'll create an estimate for 200 bags of Basmati Rice at ₹2800/bag.
           Reply yes to confirm or no to cancel."
Customer: "yes"
Bot:      "Done! Your estimate has been created."
```

**Idempotency:** Each action has a unique 12-char hex ID. Duplicate confirmations
(double-tap, WhatsApp retry) are silently ignored.

**Expiry:** Pending confirmations expire after `CONFIRMATION_TIMEOUT` seconds (default 10 min).

#### Approval Commands (Operator)

Send these on the escalation WhatsApp number:

```
APPROVE <action_id>              Clean approval — no changes
APPROVE <action_id> qty=300      Approval with modification note (resets trust streak)
REJECT <action_id>               Rejection (customer notified)
```

### 8.4 Multi-Step Workflows

Chains multiple write actions across turns without the customer repeating information.

**Currently implemented:** Quote-to-Order

```
Turn 1: "I need a quote for 500 bags of rice at ₹2800"
Bot:    → creates estimate QT-000001
        → "Done! Say accept when you're ready to place the order."

Turn 3: "I'll take it"   ← could be hours or days later
Bot:    → "I'll convert QT-000001 into a sales order. Confirm?"

Turn 4: "yes"
Bot:    → "Sent for approval. I'll let you know."

Turn 5: Operator: "APPROVE <id>"
Bot:    → SO created → "Your order has been approved!"
```

**Durability:** Workflow state is in SQLite — survives backend restart between any turns.

**Expiry:** Workflows expire after `WORKFLOW_TTL` seconds (default 24h).

**Workflow messages understood at any step:**
- `accept` / `I'll take it` / `place order` → advance to next step
- `status` / `when` / `where` → status update for current step
- `cancel` / `no` / `never mind` → cancel and fail workflow

### 8.5 Identity and Trust

Every message is matched to a Zoho CRM contact before any data is accessed.

**Three identity states:**

| State | How matched | Data access |
|---|---|---|
| `internal` | Phone number in `STAFF_JIDS` env var | All 10 read tools + all write tools |
| `known` | Phone matches a CRM Contact's phone field | 5 customer-scoped read tools + write tools |
| `unknown` | Not found in CRM | KB/general questions only, no Zoho data |

**Scoping (known customers):**
- Books list tools: `customer_name` injected into every query, overriding anything the model generated
- Inventory get tools: record ownership verified after fetch
- Even if customer asks for another company's invoices, they receive only their own

**Identity cache:** 1 hour (configurable via `IDENTITY_CACHE_TTL`). First message from a new number makes 2 CRM calls (phone lookup + Books contact lookup).

### 8.6 Operational Visibility (Staff Only)

Internal users can ask operational questions answered from local SQLite — no Zoho
network call, instant response.

**Triggered by:** `ops_query` intent (blocked for non-internal users)

**Example queries:**
- "How many quotes did we create today?"
- "What's pending approval?"
- "Any workflow failures?"
- "What did we do this week?"
- "Any alerts?"

**Data sources:**
- `action_audit` table — all write actions with timestamps
- `pending_actions` table — awaiting confirmation or approval
- `workflows` table — active/failed workflow states

### 8.7 Daily Digest

The system pushes a daily WhatsApp message to all staff JIDs at `DIGEST_HOUR`.

**Sample digest:**
```
📊 Daily Operations — 26 Jun 2026

Quotes Created    : 3
Sales Orders      : 1
Active Workflows  : 2
Pending Approvals : 0

Recent Activity:
  • 11:35 — King (Sample) — Estimate created
  • 11:18 — King (Sample) — Estimate created
  • 10:37 — King (Sample) — Estimate created

✅ No alerts
```

**Alerts included when:**
- Any approval pending longer than `APPROVAL_ALERT_HOURS` (default 4h)
- Any estimate unaccepted for longer than `WORKFLOW_STALE_HOURS` (default 24h)
- Any workflow failed in the last 24 hours

**Trigger manually (for testing):**
```powershell
Invoke-RestMethod -Uri http://localhost:8000/digest/now -Method Post
```

### 8.8 Graduated Autonomy

High-risk write tools (requiring human approval) can be promoted to low-risk
(auto-execute after customer confirm) based on a clean approval track record.

**Promotion:** `GRADUATION_THRESHOLD` consecutive clean approvals  
**Demotion:** `DEGRADE_THRESHOLD` consecutive rejections post-graduation

See [Section 12](#12-the-learning-loop) for the full promotion path.

---

## 9. API Endpoints

### Query

| Method | Path | Description |
|---|---|---|
| `POST` | `/query` | Main message handler — all customer and staff messages |
| `GET` | `/health` | Health check |

**`POST /query` body:**
```json
{
  "message":     "show my invoices",
  "sender":      "919876543210@s.whatsapp.net",
  "history":     [{"role": "user", "content": "..."}, ...],
  "quoted_text": null
}
```

**`POST /query` response:**
```json
{
  "response":         "You have no unpaid invoices.",
  "route":            "zoho",
  "source_chunks":    0,
  "latency_ms":       1677,
  "english_message":  "show my invoices",
  "english_response": "You have no unpaid invoices.",
  "rewritten_message": "Show all unpaid invoices for my account"
}
```

**Route values:**
| Route | Meaning |
|---|---|
| `rag` | Answered from ChromaDB knowledge base |
| `zoho` | Answered from live Zoho data |
| `write_proposal` | Write action proposed, awaiting confirmation |
| `zoho_write` | Write action executed successfully |
| `awaiting_approval` | High-risk action queued for operator |
| `ops` | Operational metrics answer (internal only) |
| `workflow_status` | Workflow status update |
| `general` | General LLM answer |
| `escalate` | Human notification sent |
| `cancelled` | Pending action or workflow cancelled |
| `unknown` | Sender not found in CRM |
| `re_prompt` | Confirmation response was ambiguous |

### Escalation

| Method | Path | Description |
|---|---|---|
| `POST` | `/escalate/notify` | Create escalation record (called by bot) |
| `POST` | `/escalate/{id}/message-id` | Store WhatsApp message ID for matching |
| `POST` | `/escalate/resolve` | Deliver answer or process APPROVE/REJECT |

**APPROVE/REJECT format** (sent to `/escalate/resolve`):
```json
{
  "answer":              "APPROVE abc123def456",
  "notification_msg_id": ""
}
```

### Outbox

| Method | Path | Description |
|---|---|---|
| `GET` | `/outbox/pending` | Pending outbound messages (polled by bot every 30s) |
| `POST` | `/outbox/{id}/delivered` | Mark message as delivered |
| `POST` | `/outbox/{id}/failed` | Mark message as failed |

### Operations

| Method | Path | Description |
|---|---|---|
| `GET` | `/ops/summary` | Full operational summary (counts, alerts, graduation, feedback) |
| `POST` | `/digest/now` | Manually trigger and queue a digest for all staff JIDs |

---

## 10. The Approval Flow

### Low-risk actions

```
Customer message
  → write_zoho intent
  → generate_proposal() → proposal text stored in pending_actions
  → bot sends proposal + "Reply yes to confirm or no to cancel"
  → Customer replies "yes"
  → execute_with_retry() → Zoho API → result
  → audit.log_action()
  → "Done! I've created that for you."
```

### High-risk actions

```
Customer message
  → write_zoho intent
  → generate_proposal()
  → bot sends proposal + "Reply yes to confirm or no to cancel"
  → Customer replies "yes"
  → Pending action status: awaiting_approval
  → Escalation sent to ESCALATION_JID:
      "⚠️ APPROVAL NEEDED
       Customer: [name] ([account])
       Proposed action: [description]
       Reply: APPROVE <id> or REJECT <id>"
  → Operator replies "APPROVE <id>"
  → execute_with_retry() → Zoho API
  → audit.log_action() with approved_by
  → learning.record_feedback() → updates trust streak
  → Customer notified: "Your request has been approved!"
```

### Modification approval

```
Operator replies: "APPROVE abc123def456 reduce qty by half"
                                        ↑ modification note
  → decision = "modified" (resets trust streak)
  → modification stored in action_feedback table
```

---

## 11. The Workflow State Machine

### Quote-to-Order

```
         ESTIMATE_CREATED
         ┌─────────────────────────────────────────┐
         │ Triggered when estimate write succeeds   │
         │ Context: estimate_id, estimate_number,   │
         │          line_items                      │
         └──────────────┬──────────────────────────┘
                        │ Customer says "accept" / "I'll take it"
                        ▼
         SO_PENDING_APPROVAL
         ┌─────────────────────────────────────────┐
         │ Customer confirmed SO proposal           │
         │ Escalation sent to operator              │
         └──────────────┬──────────────────────────┘
                        │ Operator sends "APPROVE <id>"
                        ▼
         COMPLETED
         ┌─────────────────────────────────────────┐
         │ SO created in Zoho Books                 │
         │ Context updated with salesorder_id       │
         └─────────────────────────────────────────┘
```

**Failure paths:**
- Any `execute_with_retry()` failure → `workflow.fail()` with `failure_reason`
- Customer cancels at any step → `workflow.fail()` with "cancelled by customer"
- TTL exceeded → auto-expired, status = `expired`

**Messages recognised at any active step:**
```
"accept" / "I'll take it" / "place order"  → accept_quote (ESTIMATE_CREATED only)
"status" / "when" / "update"               → status_check (any step)
"cancel" / "no" / "never mind"             → cancel_workflow (any step)
[anything else]                            → falls through to normal intent
```

---

## 12. The Learning Loop

### Trust Level Lifecycle

```
New high-risk tool (e.g. ZohoBooks_create_sales_order)
        │
        │  risk = "high"
        │  Requires: customer confirm → operator APPROVE
        │
        ├── Clean approval (no modification)   → streak +1
        ├── Approval with modification          → streak reset to 0
        └── Rejection                           → streak reset to 0

        When streak ≥ GRADUATION_THRESHOLD (default 5):
        │
        │  risk = "low"  ← GRADUATED
        │  Requires: customer confirm only (no operator)
        │
        ├── Clean approval    → streak continues
        ├── Modification      → streak reset (stays low)
        └── Rejection × DEGRADE_THRESHOLD (default 2):
                │
                │  risk = "high"  ← DEMOTED
                └── Returns to top
```

### Graduation Scope

Trust levels are tracked per **tool + account** combination:
- `ZohoBooks_create_sales_order::King (Sample)` — specific account
- `ZohoBooks_create_sales_order::*` — wildcard, applies to all accounts

A wildcard graduation (from seeding with `account_name=None`) promotes the tool
for all customers at once.

### Checking Graduation Status

```powershell
$s = Invoke-RestMethod -Uri http://localhost:8000/ops/summary
$s.graduation | Format-Table tool_name, account_name, clean_streak, risk_level, graduated_at
```

### Manual Trust Operations

```python
# Check effective risk before a write
from zoho_mcp.learning import get_effective_risk
get_effective_risk("ZohoBooks_create_sales_order", "King (Sample)")   # "high" or "low"

# Manually graduate a tool (for seeding / testing)
from zoho_mcp.learning import record_feedback
for i in range(5):
    record_feedback(f"seed{i}", "ZohoBooks_create_sales_order", "Acme Exports", "approved")

# Reset all trust data
import sqlite3
con = sqlite3.connect("./data/write_actions.db")
con.execute("DELETE FROM trust_levels")
con.execute("DELETE FROM action_feedback")
con.commit()
```

---

## 13. Eval and Regression Suite

The eval harness measures tool-selection accuracy against a 40-command corpus.
Run it before and after any change to prompts or model configuration.

### Running the Eval

```bash
cd backend
python -m zoho_mcp.run_eval
```

**Gate:** ≥ 95% tool-selection accuracy must be maintained.

### Adding a Production Failure to the Corpus

When a write fails, the logs contain:
```
[learning] ADD TO CORPUS: {"id":"P12345","command":"...","expected_tool":"..."}
```

1. Copy that JSON line
2. Add it to `backend/zoho_mcp/corpus.jsonl`
3. Re-run the eval: `python -m zoho_mcp.run_eval`
4. If accuracy drops below 95%, fix the prompt before merging

### Offline Eval (no MCP connection needed)

```bash
python -m zoho_mcp.run_eval --schema-file eval_results/tools_schema.json
```

### Smoke Testing Individual Tools

```bash
# List all tools on the read server
python -m zoho_mcp.smoke_test list

# Call a specific read tool
python -m zoho_mcp.smoke_test call ZohoBooks_list_invoices \
  '{"query_params": {"organization_id": "YOUR_ORG_ID", "filter_by": "Status.Unpaid"}}'

# Test write server tools
$env:ZOHO_MCP_URL = "YOUR_WRITE_SERVER_URL"
python -m zoho_mcp.smoke_test list
```

---

## 14. Operations and Monitoring

### Log Prefixes Reference

| Prefix | Module | Meaning |
|---|---|---|
| `[identity]` | identity.py | JID resolution result |
| `[intent]` | intent.py | Classified intent |
| `[scope]` | scope.py | Tool filtering or injection override |
| `[agent]` | agent.py | Read agent steps |
| `[write_agent]` | write_agent.py | Write proposal or execution |
| `[confirm]` | confirm.py | Pending action lifecycle |
| `[approve]` | main.py | Operator APPROVE/REJECT |
| `[audit]` | audit.py | Write audit entry |
| `[workflow]` | workflow.py | Workflow state changes |
| `[ops]` | main.py | Ops query answer |
| `[digest]` | digest.py | Digest generation/delivery |
| `[learning]` | learning.py | Feedback + graduation events |
| `[scope] injection override` | scope.py | Injection attempt blocked |
| `[learning] ADD TO CORPUS` | learning.py | Production failure to add to eval |
| `[learning] 🎓 GRADUATED` | learning.py | Tool promoted to low-risk |
| `[learning] 📉 DEMOTED` | learning.py | Tool demoted back to high-risk |

### Key Operational Checks

```powershell
# Full system status
Invoke-RestMethod -Uri http://localhost:8000/ops/summary | ConvertTo-Json -Depth 5

# What's in the outbox (pending WhatsApp deliveries)
Invoke-RestMethod -Uri http://localhost:8000/outbox/pending

# Health check
Invoke-RestMethod -Uri http://localhost:8000/health

# Trigger digest on demand
Invoke-RestMethod -Uri http://localhost:8000/digest/now -Method Post
```

### SQLite Tables Reference

All write-related state lives in `backend/data/write_actions.db`:

| Table | Contents |
|---|---|
| `pending_actions` | Awaiting customer confirmation or operator approval |
| `action_audit` | Immutable log of every executed write |
| `workflows` | Multi-step workflow states |
| `outbox` | Pending WhatsApp messages (digests, alerts) |
| `action_feedback` | Operator APPROVE/REJECT/modify events |
| `trust_levels` | Graduation status per tool-account pair |

---

## 15. Troubleshooting

### "You cannot perform this operation. Connection not authorised"

The MCP server's Books/CRM connection token has expired or was never set.  
**Fix:** MCP console → your server → Connection tab → Disconnect → Authorize → repeat OAuth.

### "You don't have permission to perform this operation" (code 104003)

The write server's restricted user doesn't have the required Books role.  
**Fix:** Zoho Books → Settings → Users & Roles → Roles → edit AI Agent role → add required permission.

### `books_id=None` in identity logs

The `ZohoBooks_list_contacts` lookup found no match for the CRM account name.  
**Fix:** Ensure the CRM Account Name exactly matches the Books contact name (character-for-character). Check in Books → Contacts.

### Tool schema 400 errors (Groq validation failures)

`_sanitize_schema` in `client.py` handles this automatically by converting integer/number/boolean/array types to string. If new tools are added with unusual schema types, errors are recovered from `failed_generation` and the correct tool is still scored.

### `[intent] ops_query` routes to escalate for staff user

The sender JID's phone number is not in `STAFF_JIDS`.  
**Fix:** Add the number (digits only, with country code, no `+`) to `STAFF_JIDS` in `.env`. Restart uvicorn.

### Workflow stuck in `SO_PENDING_APPROVAL`

The escalation message was sent but never approved. Check `pending_actions` table:
```python
import sqlite3
con = sqlite3.connect("./data/write_actions.db")
for r in con.execute("SELECT id, account_name, proposal_text, created_at FROM pending_actions WHERE status='awaiting_approval'").fetchall():
    print(r)
```
Then: `APPROVE <id>` via the escalation WhatsApp number.

### Identity cache returning stale data

Force re-resolution by restarting uvicorn, or call:
```python
from zoho_mcp.identity import invalidate
invalidate("919876543210@s.whatsapp.net")
```

---

## 16. Phase Build History

| Phase | What was built | Exit gate |
|---|---|---|
| **0** | Zoho MCP connection, tool schema discovery, 40-command eval harness, `_sanitize_schema` for Groq validation | ≥ 95% tool-selection accuracy |
| **1** | Intent classifier, read agent loop (pick → execute → ground → synthesize), wired into main.py | End-to-end Zoho read working |
| **2** | JID → CRM identity resolution, 3-state machine (internal/known/unknown), tool allowlist (5 customer / 10 internal), customer_name injection, ownership verification | Injection test blocked (customer asking for Acme's invoices got their own) |
| **3** | Write agent (proposal generation), confirm-then-execute, risk tiers (low/high), approval queue via escalation system, audit log, Books customer_id lookup | Both write workflows (estimate + SO) proven end-to-end |
| **4** | Workflow state machine (Quote-to-Order), durable SQLite state, restart resilience, retry with backoff, context carry-forward across turns | 5-turn Q→O workflow ran unattended, survived restart |
| **5** | Ops query path (internal only, local SQLite), daily digest scheduler, outbox table, bot polling, alert system | Staff ops query answered in 1.7s from local data |
| **6** | Feedback store, trust level tracking per tool-account, graduation (N clean approvals → auto-execute), demotion (consecutive rejections), corpus logging for regression suite | Graduation lifecycle verified: streak → graduate → demote |

---

## Acknowledgements

Built with:
- [Baileys](https://github.com/WhiskeySockets/Baileys) — WhatsApp Web API
- [Zoho MCP](https://mcp.zoho.in) — Model Context Protocol servers for Zoho One
- [Groq](https://groq.com) — LLM inference (llama-3.3-70b-versatile, llama-3.1-8b-instant)
- [LlamaIndex](https://www.llamaindex.ai) + [ChromaDB](https://www.trychroma.com) — RAG pipeline
- [FastAPI](https://fastapi.tiangolo.com) — Backend framework
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — MCP client