# backend/app/main.py
import logging
import re
import time
import asyncio
import json
import os
from datetime import datetime
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

import app.rag as rag
import app.judge as judge
import app.escalate as escalate
import app.kb_writer as kb_writer
from app.router import classify_query
from app.general import general_llm_answer
from app.rewriter import rewrite_query
from app.config import ESCALATION_HOLDING_MESSAGE, ESCALATION_JID
import app.translator as translator
import zoho_mcp.intent    as intent_classifier
import zoho_mcp.agent     as zoho_agent
import zoho_mcp.identity  as identity_resolver
import zoho_mcp.confirm     as confirm
import zoho_mcp.audit       as audit
import zoho_mcp.write_agent as write_agent
import zoho_mcp.workflow    as workflow
import zoho_mcp.ops         as ops
import zoho_mcp.digest      as digest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting up — loading RAG pipeline...")
    rag.load_index()
    log.info("RAG pipeline ready ✅")
    escalate.init_db()
    log.info("Escalation DB ready ✅")
    confirm.init_db()
    audit.init_db()
    workflow.init_db()
    digest.init_db()
    log.info("Write action DB ready ✅")

    # Start background digest scheduler
    _task = asyncio.create_task(_digest_scheduler())

    yield

    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    yield
    log.info("Shutting down.")


app = FastAPI(title="WhatsApp RAG Bot", lifespan=lifespan)


# ── Background digest scheduler ───────────────────────────────────────────────

async def _digest_scheduler() -> None:
    """
    Sends the daily operations digest at DIGEST_HOUR (default 9 AM).
    Runs as a background asyncio task started in lifespan.
    Uses a set to prevent double-sending on the same calendar day.
    """
    from zoho_mcp.identity import _STAFF_PHONES
    digest_hour  = int(os.getenv("DIGEST_HOUR", "9"))
    _sent_today: set[str] = set()

    while True:
        try:
            await asyncio.sleep(60)    # check every minute
            now     = datetime.now()
            day_key = now.strftime("%Y-%m-%d")

            if now.hour == digest_hour and day_key not in _sent_today:
                log.info("[digest] triggering daily digest for %d staff JIDs",
                         len(_STAFF_PHONES))
                text = digest.generate_digest_text()
                for phone in _STAFF_PHONES:
                    jid = f"{phone}@s.whatsapp.net"
                    mid = digest.schedule_to_outbox(jid, text)
                    log.info("[digest] queued mid=%s for %s", mid, jid)
                _sent_today.add(day_key)
                # Prevent set growing unboundedly
                if len(_sent_today) > 7:
                    _sent_today.discard(min(_sent_today))

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[digest] scheduler error: %s", exc)


# ── Ops query LLM synthesis ───────────────────────────────────────────────────

async def _answer_ops_query(message: str) -> str:
    """
    Answer an operational question from local SQLite data using Groq.
    Returns a concise natural-language answer.
    """
    from groq import Groq
    from zoho_mcp.config import GROQ_API_KEY, AGENT_MODEL

    context = ops.get_daily_summary()
    context["alerts"] = ops.get_alerts()

    client = Groq(api_key=GROQ_API_KEY)
    system = (
        "You are an operations assistant for a B2B merchant-export company. "
        "Answer the question using ONLY the operational data provided. "
        "Be concise (2-4 sentences). "
        "Format monetary values as ₹ with Indian comma formatting. "
        "Refer to times as HH:MM. "
        "Never mention internal IDs or database field names."
    )
    user = (
        f"Question: {message}\n\n"
        f"Operational data:\n{json.dumps(context, indent=2, default=str)}"
    )
    try:
        resp = client.chat.completions.create(
            model=AGENT_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            temperature=0.1,
            max_tokens=256,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        log.error("[ops] LLM synthesis failed: %s", exc)
        return (
            f"Today: {context.get('quotes_created', 0)} quotes created, "
            f"{context.get('orders_created', 0)} sales orders, "
            f"{context.get('pending_approvals', 0)} pending approvals."
        )
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic models ───────────────────────────────────────────────────────────

class HistoryMessage(BaseModel):
    role: str
    content: str


class QueryRequest(BaseModel):
    message: str
    sender: str = "anonymous"
    history: Optional[List[HistoryMessage]] = []
    quoted_text: Optional[str] = None


class QueryResponse(BaseModel):
    response: str
    source_chunks: int
    latency_ms: int
    route: str
    rewritten_message: str | None = None
    english_message: str | None = None    # English version of user input  (for history)
    english_response: str | None = None   # English version of bot response (for history)


class EscalationNotifyRequest(BaseModel):
    customer_jid: str
    question: str
    customer_msg_id: str | None = None


class EscalationNotifyResponse(BaseModel):
    escalation_id: str
    escalation_jid: str


class EscalationResolveRequest(BaseModel):
    answer: str
    notification_msg_id: str | None = None


class EscalationResolveResponse(BaseModel):
    customer_jid: str
    question: str
    answer: str
    chunks_written: int
    customer_msg_id: str | None = None


class SetMessageIdRequest(BaseModel):
    notification_msg_id: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def build_enriched_message(message: str, history: list) -> str:
    if not history:
        return message
    history_text = "\n".join(
        f"{'User' if h.role == 'user' else 'Assistant'}: {h.content}"
        for h in history
    )
    return (
        f"Previous conversation:\n{history_text}\n\n"
        f"Current question: {message}"
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "index_loaded": rag._index is not None,
        "pending_escalations": escalate.get_pending_count(),
    }


@app.post("/query", response_model=QueryResponse)
@limiter.limit("10/minute")
async def query_endpoint(request: Request, body: QueryRequest):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    if rag._index is None:
        raise HTTPException(status_code=503, detail="Index not ready")

    log.info(f"Query from {body.sender}: '{body.message}'")
    start = time.time()

    try:
        # ── Step 1: Translate input to English ────────────────────────────────
        english_message, source_language, source_script = translator.detect_and_translate(body.message)

        # Translate quoted text too — it may be in the user's language
        english_quoted = None
        if body.quoted_text:
            english_quoted, _, _ = translator.detect_and_translate(body.quoted_text)

        # ── Step 2: Build enriched message (history is already in English) ────
        enriched = build_enriched_message(english_message, body.history)

        # ── Step 3: Rewrite incomplete query using conversation context ───────
        rewritten = rewrite_query(english_message, body.history, english_quoted)

        # ── Step 4: Resolve sender identity ──────────────────────────────────
        identity = await identity_resolver.resolve(body.sender)
        log.info(f"  [identity] state={identity.state} "
                 f"account={identity.account_name or '—'} "
                 f"phone={identity.phone}")

        history_dicts = [{"role": h.role, "content": h.content}
                         for h in body.history]

        # ── Step 5: Check for pending write confirmation ──────────────────────
        # Pending confirmations take priority over everything else.
        pending = confirm.get_pending(body.sender)

        if pending:
            decision = confirm.parse_response(rewritten)
            log.info(f"  [confirm] pending={pending.id} decision={decision}")

            if decision == "cancelled":
                confirm.update_status(pending.id, "cancelled")
                wf = workflow.get_active(body.sender)
                if wf:
                    workflow.fail(wf.id, "cancelled by customer during confirmation")
                english_response = "No problem, I've cancelled that request."
                route            = "cancelled"
                source_chunks    = 0

            elif decision == "confirmed":
                if pending.risk == "low":
                    confirm.update_status(pending.id, "confirmed")
                    result_text = await write_agent.execute_with_retry(
                        pending.tool_name, pending.tool_args,
                        books_customer_id=identity.books_customer_id,
                    )
                    if result_text:
                        confirm.update_status(pending.id, "executed")
                        audit.log_action(
                            jid=body.sender,
                            account_name=identity.account_name,
                            action_id=pending.id,
                            risk="low",
                            tool_name=pending.tool_name,
                            tool_args=pending.tool_args,
                            result_summary="executed successfully",
                            zoho_response=result_text,
                        )
                        # ── Phase 4: start workflow after estimate creation ────
                        if pending.tool_name == "ZohoBooks_create_estimate":
                            wf = workflow.start_quote_to_order(
                                jid=body.sender,
                                account_name=identity.account_name,
                                result_text=result_text,
                                tool_args=pending.tool_args,
                            )
                            english_response = (
                                "Done! Your estimate has been created. "
                                "When you're ready to place the order, "
                                "just say *accept* or *place order*."
                                if wf else "Done! I've created that for you."
                            )
                            if wf:
                                log.info("  [workflow] quote_to_order started id=%s", wf.id)
                        else:
                            english_response = "Done! I've created that for you."
                        route         = "zoho_write"
                        source_chunks = 0
                    else:
                        wf = workflow.get_active(body.sender)
                        if wf:
                            workflow.fail(wf.id, f"{pending.tool_name} failed after retries")
                        english_response = ESCALATION_HOLDING_MESSAGE
                        route            = "escalate"
                        source_chunks    = 0

                else:
                    confirm.update_status(pending.id, "awaiting_approval")
                    wf = workflow.get_active(body.sender)
                    if wf and pending.tool_name == "ZohoBooks_create_sales_order":
                        workflow.advance(wf.id, workflow.SO_PENDING_APPROVAL)
                    approval_msg = (
                        f"⚠️ APPROVAL NEEDED\n\n"
                        f"Customer: {identity.contact_name or 'Unknown'} "
                        f"({identity.account_name or body.sender})\n\n"
                        f"Proposed action:\n{pending.proposal_text}\n\n"
                        f"Reply:\n"
                        f"  APPROVE {pending.id}\n"
                        f"  REJECT {pending.id}"
                    )
                    escalate.create_escalation(body.sender, approval_msg, None)
                    english_response = (
                        "Got it — I've sent that for approval. "
                        "I'll let you know as soon as it's confirmed."
                    )
                    route         = "awaiting_approval"
                    source_chunks = 0

            else:
                english_response = (
                    f"Just to confirm — {pending.proposal_text}\n\n"
                    "Reply *yes* to proceed or *no* to cancel."
                )
                route         = "re_prompt"
                source_chunks = 0

        else:
            # ── Step 5b: Check for active workflow ────────────────────────────
            active_wf  = workflow.get_active(body.sender)
            wf_handled = False

            if active_wf:
                wf_intent = workflow.classify_in_context(rewritten, active_wf)
                log.info("  [workflow] id=%s step=%s wf_intent=%s",
                         active_wf.id, active_wf.current_step, wf_intent)

                if wf_intent == "accept_quote":
                    ctx        = active_wf.context
                    est_num    = ctx.get("estimate_number", "the estimate")
                    est_id     = ctx.get("estimate_id", "")
                    line_items = ctx.get("line_items", [])
                    so_args = {
                        "body": {
                            "customer_id": "customer_id",
                            "estimate_id": est_id,
                            "line_items":  line_items,
                        },
                        "query_params": {"organization_id": "organization_id"},
                    }
                    proposal_text = (
                        f"I'll convert {est_num} into a confirmed sales order. "
                        f"This commits the order — shall I proceed?"
                    )
                    confirm.create_pending(
                        jid=body.sender,
                        account_name=identity.account_name,
                        risk="high",
                        tool_name="ZohoBooks_create_sales_order",
                        tool_args=so_args,
                        proposal_text=proposal_text,
                    )
                    english_response = (
                        f"{proposal_text}\n\nReply *yes* to confirm or *no* to cancel."
                    )
                    route         = "write_proposal"
                    source_chunks = 0
                    wf_handled    = True

                elif wf_intent == "status_check":
                    step = active_wf.current_step
                    if step == workflow.ESTIMATE_CREATED:
                        est_num = active_wf.context.get("estimate_number", "your estimate")
                        english_response = (
                            f"{est_num} has been created and is ready. "
                            f"Say *accept* when you'd like to place the order."
                        )
                    elif step == workflow.SO_PENDING_APPROVAL:
                        english_response = (
                            "Your sales order is waiting for approval from our team. "
                            "I'll notify you as soon as it's confirmed."
                        )
                    else:
                        english_response = "Your request is being processed."
                    route         = "workflow_status"
                    source_chunks = 0
                    wf_handled    = True

                elif wf_intent == "cancel_workflow":
                    workflow.fail(active_wf.id, "cancelled by customer")
                    english_response = "No problem — I've cancelled that workflow."
                    route         = "cancelled"
                    source_chunks = 0
                    wf_handled    = True

            if not wf_handled:
                # ── Step 6: Classify intent ───────────────────────────────────
                intent = intent_classifier.classify(rewritten, history_dicts)
                log.info(f"  [intent] {intent}")

                if intent == "write_zoho":
                    if identity.state == "unknown":
                        english_response = (
                            "I wasn't able to find your number in our system. "
                            "Please contact us to get set up with account access."
                        )
                        route         = "unknown"
                        source_chunks = 0
                    else:
                        proposal = await write_agent.generate_proposal(
                            rewritten, history_dicts, identity
                        )
                        if proposal:
                            tool_name, proposal_text, tool_args, risk = proposal
                            confirm.create_pending(
                                jid=body.sender,
                                account_name=identity.account_name,
                                risk=risk,
                                tool_name=tool_name,
                                tool_args=tool_args,
                                proposal_text=proposal_text,
                            )
                            english_response = (
                                f"{proposal_text}\n\nReply *yes* to confirm or *no* to cancel."
                            )
                            route         = "write_proposal"
                            source_chunks = 0
                        else:
                            english_response = ESCALATION_HOLDING_MESSAGE
                            route            = "escalate"
                            source_chunks    = 0

                elif intent == "read_zoho":
                    if identity.state == "unknown":
                        english_response = (
                            "I wasn't able to find your number in our system. "
                            "Please contact us to get set up with account access. "
                            "I'm happy to help with general questions in the meantime!"
                        )
                        route         = "unknown"
                        source_chunks = 0
                    else:
                        zoho_answer = await zoho_agent.run(
                            rewritten, history_dicts, identity
                        )
                        if zoho_answer:
                            english_response = zoho_answer
                            route            = "zoho"
                            source_chunks    = 0
                        else:
                            english_response = ESCALATION_HOLDING_MESSAGE
                            route            = "escalate"
                            source_chunks    = 0

                elif intent == "general":
                    english_response = general_llm_answer(english_message, body.history)
                    route            = "general"
                    source_chunks    = 0

                elif intent == "ops_query":
                    # Operational metrics — only available to internal (staff) users.
                    # Answered from local SQLite (audit log, workflows, pending actions)
                    # — no Zoho network call needed, instant response.
                    if identity.state != "internal":
                        log.warning(
                            "[ops] ops_query attempted by non-internal user %s",
                            identity.state,
                        )
                        english_response = ESCALATION_HOLDING_MESSAGE
                        route            = "escalate"
                    else:
                        english_response = await _answer_ops_query(rewritten)
                        route            = "ops"
                    source_chunks = 0

                elif intent == "escalate":
                    english_response = ESCALATION_HOLDING_MESSAGE
                    route            = "escalate"
                    source_chunks    = 0

                else:
                    nodes      = rag.retrieve(rewritten)
                    sufficient = judge.is_sufficient(rewritten, nodes)
                    log.info(f"  [judge] sufficient={sufficient}")
                    if sufficient:
                        english_response = rag.synthesize(enriched, nodes)
                        route            = "rag"
                        source_chunks    = len(nodes)
                    else:
                        english_response = ESCALATION_HOLDING_MESSAGE
                        route            = "escalate"
                        source_chunks    = 0

                # ── Step 6: Translate response back to user's language ────────────────
        final_response = translator.translate_to_language(english_response, source_language, source_script)

        latency = int((time.time() - start) * 1000)
        log.info(f"  [{route}] Answer ({latency}ms): '{final_response[:100]}...'")

        return QueryResponse(
            response=final_response,
            source_chunks=source_chunks,
            latency_ms=latency,
            route=route,
            rewritten_message=rewritten if rewritten != english_message else None,
            # Only set when translation occurred — handler.js uses these for history
            english_message=english_message if source_language != "english" else None,
            english_response=english_response if source_language != "english" else None,
        )

    except Exception as e:
        log.error(f"Query failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/escalate/notify", response_model=EscalationNotifyResponse)
async def escalate_notify(body: EscalationNotifyRequest):
    if not ESCALATION_JID:
        raise HTTPException(status_code=503, detail="ESCALATION_JID not configured")

    eid = escalate.create_escalation(body.customer_jid, body.question, body.customer_msg_id)
    log.info(f"  [/escalate/notify] id={eid} customer={body.customer_jid}")

    return EscalationNotifyResponse(
        escalation_id=eid,
        escalation_jid=ESCALATION_JID,
    )


@app.post("/escalate/{escalation_id}/message-id")
async def set_escalation_message_id(escalation_id: str, body: SetMessageIdRequest):
    escalate.set_notification_msg_id(escalation_id, body.notification_msg_id)
    log.info(f"  [/escalate/message-id] stored for escalation_id={escalation_id}")
    return {"ok": True}


@app.post("/escalate/resolve", response_model=EscalationResolveResponse)
async def escalate_resolve(body: EscalationResolveRequest):
    answer = body.answer.strip()

    # ── Phase 3: intercept APPROVE / REJECT commands ──────────────────────────
    # Human operator sends: "APPROVE abc123def456" or "REJECT abc123def456"
    # The action_id is the 12-char hex from confirm.make_action_id().
    approve_match = re.match(r"^APPROVE\s+([a-f0-9]{12})", answer, re.IGNORECASE)
    reject_match  = re.match(r"^REJECT\s+([a-f0-9]{12})",  answer, re.IGNORECASE)

    if approve_match or reject_match:
        action_id = (approve_match or reject_match).group(1).lower()
        action    = confirm.get_by_id(action_id)

        if not action or action.status != "awaiting_approval":
            raise HTTPException(
                status_code=404,
                detail=f"No action awaiting approval with id={action_id}",
            )

        if reject_match:
            confirm.update_status(action_id, "rejected")
            log.info(f"  [approve] REJECTED action={action_id}")
            # Notify customer of rejection via the normal escalation reply path
            row = escalate.resolve_escalation(
                "Your request has been reviewed and could not be approved at this time. "
                "Please contact us if you have questions.",
                body.notification_msg_id,
            )
            return EscalationResolveResponse(
                customer_jid=row["customer_jid"] if row else action.jid,
                question=action.proposal_text,
                answer="Rejected",
                chunks_written=0,
                customer_msg_id=row["customer_msg_id"] if row else None,
            )

        # APPROVE — execute the write
        log.info(f"  [approve] APPROVED action={action_id} by operator")
        # Re-resolve identity for the customer to get books_customer_id.
        # Will hit the cache (1-hour TTL) so no extra network call.
        approval_identity = await identity_resolver.resolve(action.jid)
        result_text = await write_agent.execute_write(
            action.tool_name, action.tool_args,
            books_customer_id=approval_identity.books_customer_id,
        )

        if result_text:
            confirm.update_status(action_id, "approved")
            audit.log_action(
                jid=action.jid,
                account_name=action.account_name,
                action_id=action_id,
                risk="high",
                tool_name=action.tool_name,
                tool_args=action.tool_args,
                result_summary="approved and executed",
                zoho_response=result_text,
                approved_by=ESCALATION_JID,
            )
            customer_answer = "Great news — your request has been approved and processed!"
        else:
            confirm.update_status(action_id, "rejected")
            customer_answer = (
                "I tried to process your approved request but encountered an error. "
                "Our team will follow up shortly."
            )

        row = escalate.resolve_escalation(customer_answer, body.notification_msg_id)
        return EscalationResolveResponse(
            customer_jid=row["customer_jid"] if row else action.jid,
            question=action.proposal_text,
            answer=customer_answer,
            chunks_written=0,
            customer_msg_id=row["customer_msg_id"] if row else None,
        )

    # ── Standard escalation resolve (unchanged from Phase 0–2) ───────────────
    row = escalate.resolve_escalation(answer, body.notification_msg_id)

    if not row:
        raise HTTPException(status_code=404, detail="No pending escalations to resolve")

    chunks_written = kb_writer.write_qa_to_kb(row["question"], answer)
    log.info(
        f"  [/escalate/resolve] delivered to {row['customer_jid']}, "
        f"{chunks_written} chunk(s) written"
    )

    return EscalationResolveResponse(
        customer_jid=row["customer_jid"],
        question=row["question"],
        answer=answer,
        chunks_written=chunks_written,
        customer_msg_id=row.get("customer_msg_id"),
    )

# ── Phase 5: Outbox endpoints ─────────────────────────────────────────────────
# The Baileys bot polls /outbox/pending every 30s and delivers queued messages.

@app.get("/outbox/pending")
async def get_pending_outbox():
    """Return pending outbound messages for the Baileys bot to deliver."""
    return {"messages": digest.get_pending_outbox()}


@app.post("/outbox/{message_id}/delivered")
async def mark_outbox_delivered(message_id: str):
    """Called by the Baileys bot after successfully sending a message."""
    digest.mark_delivered(message_id)
    return {"ok": True, "id": message_id}


@app.post("/outbox/{message_id}/failed")
async def mark_outbox_failed(message_id: str):
    """Called by the Baileys bot if delivery fails."""
    digest.mark_failed(message_id)
    return {"ok": True, "id": message_id}


# ── Phase 5: Manual digest trigger (testing / on-demand) ─────────────────────

@app.post("/digest/now")
async def trigger_digest_now():
    """
    Manually trigger a digest for all staff JIDs.
    Useful for testing Phase 5 without waiting for the scheduled hour,
    and for on-demand visibility into current pipeline state.
    """
    from zoho_mcp.identity import _STAFF_PHONES
    text = digest.generate_digest_text()
    outbox_ids = []
    for phone in _STAFF_PHONES:
        jid = f"{phone}@s.whatsapp.net"
        mid = digest.schedule_to_outbox(jid, text)
        outbox_ids.append(mid)
    log.info("[digest] manual trigger — queued %d message(s)", len(outbox_ids))
    return {"digest": text, "outbox_ids": outbox_ids, "jids": len(outbox_ids)}


@app.get("/ops/summary")
async def ops_summary():
    """Current operational summary — JSON endpoint for dashboards / debugging."""
    return {
        "summary": ops.get_daily_summary(),
        "alerts":  ops.get_alerts(),
    }