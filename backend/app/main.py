# backend/app/main.py
import logging
import re
import time
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
import zoho_mcp.confirm   as confirm
import zoho_mcp.audit     as audit
import zoho_mcp.write_agent as write_agent

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
    log.info("Write action DB ready ✅")
    yield
    log.info("Shutting down.")


app = FastAPI(title="WhatsApp RAG Bot", lifespan=lifespan)
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
        # This must happen BEFORE intent classification. If the user sent
        # "yes" or "no" in response to a pending proposal, handle it here
        # without calling the classifier.
        pending = confirm.get_pending(body.sender)

        if pending:
            decision = confirm.parse_response(rewritten)
            log.info(f"  [confirm] pending={pending.id} decision={decision}")

            if decision == "cancelled":
                confirm.update_status(pending.id, "cancelled")
                english_response = "No problem, I've cancelled that request."
                route            = "cancelled"
                source_chunks    = 0

            elif decision == "confirmed":
                if pending.risk == "low":
                    # ── Low-risk: execute immediately ─────────────────────────
                    confirm.update_status(pending.id, "confirmed")
                    result_text = await write_agent.execute_write(
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
                        english_response = "Done! I've created that for you."
                        route            = "zoho_write"
                    else:
                        english_response = ESCALATION_HOLDING_MESSAGE
                        route            = "escalate"
                    source_chunks = 0

                else:
                    # ── High-risk: send for human approval ────────────────────
                    confirm.update_status(pending.id, "awaiting_approval")
                    # Reuse the escalation system so handler.js sends the
                    # approval request to ESCALATION_JID automatically.
                    approval_msg = (
                        f"⚠️ APPROVAL NEEDED\n\n"
                        f"Customer: {identity.contact_name or 'Unknown'} "
                        f"({identity.account_name or body.sender})\n\n"
                        f"Proposed action:\n{pending.proposal_text}\n\n"
                        f"Reply:\n"
                        f"  APPROVE {pending.id}\n"
                        f"  REJECT {pending.id}"
                    )
                    escalate.create_escalation(
                        body.sender,
                        approval_msg,
                        None,
                    )
                    english_response = (
                        "Got it — I've sent that for approval. "
                        "I'll let you know as soon as it's confirmed."
                    )
                    route         = "awaiting_approval"
                    source_chunks = 0

            else:
                # Ambiguous reply — re-prompt
                english_response = (
                    f"Just to confirm — {pending.proposal_text}\n\n"
                    "Reply *yes* to proceed or *no* to cancel."
                )
                route         = "re_prompt"
                source_chunks = 0

        else:
            # ── Step 6: Classify intent ───────────────────────────────────────
            intent = intent_classifier.classify(rewritten, history_dicts)
            log.info(f"  [intent] {intent}")

            # ── Step 7: Route ─────────────────────────────────────────────────

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

            elif intent == "escalate":
                english_response = ESCALATION_HOLDING_MESSAGE
                route            = "escalate"
                source_chunks    = 0

            else:
                # answer_from_kb — RAG pipeline
                nodes     = rag.retrieve(rewritten)
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