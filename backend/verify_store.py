import chromadb
import os
from dotenv import load_dotenv

load_dotenv()
os.environ["ANONYMIZED_TELEMETRY"] = "False"

CHROMA_DIR      = os.getenv("CHROMA_PERSIST_DIR", "./chroma_store")
COLLECTION_NAME = "sop_docs"

client = chromadb.PersistentClient(path=CHROMA_DIR)
collection = client.get_collection(COLLECTION_NAME)

count = collection.count()
print(f"Total chunks in ChromaDB: {count}")

# Peek at the first stored chunk
sample = collection.peek(limit=1)
print(f"\nSample chunk:\n{sample['documents'][0][:300]}...")

# add to verify_store.py temporarily
sample = collection.peek(limit=3)
for i, doc in enumerate(sample['documents']):
    print(f"\n--- Chunk {i+1} ({len(doc.split())} words) ---")
    print(doc[:200])