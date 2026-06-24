# backend/app/rag.py
import logging
from typing import List

import chromadb
from llama_index.core import VectorStoreIndex, StorageContext, Settings, PromptTemplate
from llama_index.core.retrievers import VectorIndexRetriever
from llama_index.core.response_synthesizers import get_response_synthesizer
from llama_index.llms.groq import Groq
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.vector_stores.chroma import ChromaVectorStore

from app.config import (
    GROQ_API_KEY,
    CHROMA_PERSIST_DIR,
    COLLECTION_NAME,
    TOP_K,
    MODEL_NAME,
    EMBED_MODEL_NAME,
)

log = logging.getLogger(__name__)

QA_PROMPT_TEMPLATE = PromptTemplate(
    "You are a helpful assistant for our company.\n"
    "Answer the question using ONLY the context below.\n"
    "Do not make up or infer information not present in the context.\n"
    "Be concise and direct.\n\n"
    "Context:\n"
    "---------------------\n"
    "{context_str}\n"
    "---------------------\n\n"
    "Question: {query_str}\n"
    "Answer: "
)

# Module-level index — loaded once, reused for every query
_index = None


def load_index():
    global _index

    log.info("Loading LLM (Groq)...")
    llm = Groq(model=MODEL_NAME, api_key=GROQ_API_KEY)

    log.info("Loading embedding model...")
    embed_model = HuggingFaceEmbedding(model_name=EMBED_MODEL_NAME)

    Settings.llm = llm
    Settings.embed_model = embed_model

    log.info("Connecting to ChromaDB...")
    chroma_client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
    chroma_collection = chroma_client.get_collection(COLLECTION_NAME)
    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    log.info("Building index from existing vector store...")
    _index = VectorStoreIndex.from_vector_store(
        vector_store=vector_store,
        storage_context=storage_context,
    )
    log.info("Index ready.")


def retrieve(raw_message: str) -> List:
    """
    Retrieve the top-K chunks for raw_message from ChromaDB.

    Returns ALL top-K nodes unconditionally — no score filtering.
    The caller (judge.py) decides whether the chunks are sufficient.

    raw_message: the user's bare query, no conversation history,
                 so history tokens don't pollute the embedding space.
    """
    retriever = VectorIndexRetriever(
        index=_index,
        similarity_top_k=TOP_K,
    )
    nodes = retriever.retrieve(raw_message)

    for n in nodes:
        score = round(n.score, 3) if n.score else "N/A"
        preview = n.text[:60].replace('\n', ' ')
        log.info(f"  [retriever] score={score} | '{preview}...'")

    return nodes


def synthesize(enriched_message: str, nodes: List) -> str:
    """
    Synthesize an answer from pre-approved nodes.

    Only called after the judge has confirmed the nodes are sufficient.
    enriched_message includes conversation history for follow-up coherence.

    Returns the answer string.
    """
    synthesizer = get_response_synthesizer(
        text_qa_template=QA_PROMPT_TEMPLATE,
    )
    response = synthesizer.synthesize(enriched_message, nodes=nodes)
    return str(response)