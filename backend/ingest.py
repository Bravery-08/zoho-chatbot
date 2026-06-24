# backend/ingest.py
import os
import re
import shutil
import argparse
from dotenv import load_dotenv
from llama_index.core import VectorStoreIndex, StorageContext, Document
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.vector_stores.chroma import ChromaVectorStore
import chromadb
from app.config import CHUNK_SIZE, CHUNK_OVERLAP


load_dotenv()
os.environ["ANONYMIZED_TELEMETRY"] = "False"

DATA_DIR        = os.getenv("DATA_DIR", "./data")
CHROMA_DIR      = os.getenv("CHROMA_PERSIST_DIR", "./chroma_store")
COLLECTION_NAME = "sop_docs"


def load_documents(data_dir: str) -> list[Document]:
    """
    Load text files from data_dir. Files structured with SOP-XXX sections are
    split on section boundaries so chunks never span two SOPs.
    """
    documents = []

    for filename in sorted(os.listdir(data_dir)):
        filepath = os.path.join(data_dir, filename)
        if not os.path.isfile(filepath):
            continue

        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # Split on the divider line that immediately precedes each "SOP-XXX:" heading.
        # The lookahead keeps "SOP-" at the start of every section part.
        parts = re.split(r"\n-{20,}\n(?=SOP-\d+:)", content)
        sop_sections = [p.strip() for p in parts if re.match(r"SOP-\d+:", p.strip())]

        if sop_sections:
            for section in sop_sections:
                documents.append(Document(text=section, metadata={"source": filename}))
            print(f"  {filename}: split into {len(sop_sections)} SOP sections")
        else:
            documents.append(Document(text=content, metadata={"source": filename}))
            print(f"  {filename}: loaded as single document")

    return documents


def main(force_reindex=False):
    if force_reindex and os.path.exists(CHROMA_DIR):
        print("Force reindex — clearing existing vector store...")
        shutil.rmtree(CHROMA_DIR)

    print("Step 1: Loading documents...")
    documents = load_documents(DATA_DIR)
    print(f"  {len(documents)} section(s) loaded")

    print("Step 2: Splitting into chunks...")
    splitter = SentenceSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    nodes = splitter.get_nodes_from_documents(documents)
    print(f"  {len(nodes)} chunks created")

    print("Step 3: Loading embedding model...")
    embed_model = HuggingFaceEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")

    print("Step 4: Setting up ChromaDB...")
    chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    chroma_collection = chroma_client.get_or_create_collection(COLLECTION_NAME)
    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    print("Step 5: Embedding and storing chunks...")
    VectorStoreIndex(nodes, storage_context=storage_context, embed_model=embed_model)

    print(f"\nDone! {len(nodes)} chunks stored in ChromaDB at '{CHROMA_DIR}'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Wipe and rebuild the vector store from scratch")
    args = parser.parse_args()
    main(force_reindex=args.force)
