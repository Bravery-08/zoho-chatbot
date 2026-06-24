# backend/app/kb_writer.py
import logging
import os
from datetime import datetime
from llama_index.core import Document
from llama_index.core.node_parser import SentenceSplitter
import app.rag as rag
from app.config import ESCALATION_LOG_PATH, CHUNK_SIZE, CHUNK_OVERLAP

log = logging.getLogger(__name__)


def write_qa_to_kb(question: str, answer: str) -> int:
    """
    Embed a resolved Q&A pair directly into the live ChromaDB collection
    and append it to the escalation log file for audit.

    Returns the number of chunks written (normally 1 for a short Q&A).
    The live index picks up the new chunks on the next retrieve() call
    — no restart or reload needed.
    """
    # Format as a clean Q&A document so the embedding captures both parts
    qa_text = (
        f"Q: {question.strip()}\n"
        f"A: {answer.strip()}"
    )

    # ── 1. Embed and insert into ChromaDB ────────────────────────────────────
    document = Document(
        text=qa_text,
        metadata={"source": "escalation", "written_at": datetime.utcnow().isoformat()}
    )

    splitter = SentenceSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    nodes = splitter.get_nodes_from_documents([document])

    # rag._index is the live VectorStoreIndex loaded at startup
    # insert() embeds the nodes and writes them to the ChromaDB collection
    rag._index.insert_nodes(nodes)

    log.info(f"  [kb_writer] inserted {len(nodes)} chunk(s) into ChromaDB")

    # ── 2. Append to escalation log file ─────────────────────────────────────
    os.makedirs(os.path.dirname(ESCALATION_LOG_PATH), exist_ok=True)
    with open(ESCALATION_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n---\nDate: {datetime.utcnow().isoformat()}\n")
        f.write(f"Q: {question.strip()}\n")
        f.write(f"A: {answer.strip()}\n")

    log.info(f"  [kb_writer] appended to {ESCALATION_LOG_PATH}")
    return len(nodes)