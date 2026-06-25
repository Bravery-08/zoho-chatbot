# backend/app/main.py
import logging
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
import zoho_mcp.intent as intent_classifier
import zoho_mcp.agent as zoho_agent

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

        # ── Step 4: Classify intent ───────────────────────────────────────────
        # Four-way: answer_from_kb | read_zoho | general | escalate
        # Uses the fast 8B model — cheap routing call, not an answer.
        history_dicts = [{"role": h.role, "content": h.content} for h in body.history]
        intent = intent_classifier.classify(rewritten, history_dicts)
        log.info(f"  [intent] {intent}")

        if intent == "read_zoho":
            # ── Zoho agent path ───────────────────────────────────────────────
            # pick tool → inject org_id → call Zoho → ground → synthesize
            zoho_answer = await zoho_agent.run(rewritten, history_dicts)
            if zoho_answer:
                english_response = zoho_answer
                route            = "zoho"
                source_chunks    = 0
            else:
                # grounding failed or no tool matched → human escalation
                english_response = ESCALATION_HOLDING_MESSAGE
                route            = "escalate"
                source_chunks    = 0

        elif intent == "general":
            english_response = general_llm_answer(english_message, body.history)
            route            = "general"
            source_chunks    = 0

        elif intent == "escalate":
            # intent classifier directly flagged for human
            english_response = ESCALATION_HOLDING_MESSAGE
            route            = "escalate"
            source_chunks    = 0

        else:
            # answer_from_kb — go through the existing RAG pipeline
            # ── Step 5: Retrieve chunks ───────────────────────────────────────
            nodes = rag.retrieve(rewritten)

            # ── Step 6: LLM judge ─────────────────────────────────────────────
            sufficient = judge.is_sufficient(rewritten, nodes)
            log.info(f"  [judge] sufficient={sufficient}")

            if sufficient:
                english_response = rag.synthesize(enriched, nodes)
                route            = "rag"
                source_chunks    = len(nodes)
            else:
                # KB doesn't have it — escalate (intent said it was company
                # knowledge, so the human should see it)
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
    row = escalate.resolve_escalation(body.answer, body.notification_msg_id)

    if not row:
        raise HTTPException(status_code=404, detail="No pending escalations to resolve")

    chunks_written = kb_writer.write_qa_to_kb(row["question"], body.answer)
    log.info(
        f"  [/escalate/resolve] delivered to {row['customer_jid']}, "
        f"{chunks_written} chunk(s) written"
    )

    return EscalationResolveResponse(
        customer_jid=row["customer_jid"],
        question=row["question"],
        answer=body.answer,
        chunks_written=chunks_written,
        customer_msg_id=row.get("customer_msg_id"),
    )