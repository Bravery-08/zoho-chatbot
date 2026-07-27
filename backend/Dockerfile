FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl \
    && rm -rf /var/lib/apt/lists/*

# Layer 1 — web framework (pinned to match local env)
RUN pip install --no-cache-dir --timeout 120 --retries 5 \
    "fastapi==0.111.0" \
    "starlette==0.37.2" \
    "sse-starlette>=1.6.1,<2" \
    "uvicorn[standard]" \
    "python-dotenv" \
    "python-multipart" \
    "requests"

# Layer 2 — LLM clients
RUN pip install --no-cache-dir --timeout 120 --retries 5 \
    "groq==0.9.0" \
    "openai"

# Layer 3 — ChromaDB
RUN pip install --no-cache-dir --timeout 120 --retries 5 \
    "chromadb==0.5.3"

# Layer 4 — LlamaIndex core + integrations
RUN pip install --no-cache-dir --timeout 180 --retries 5 \
    "llama-index-core==0.10.43" \
    "llama-index-vector-stores-chroma==0.1.10" \
    "llama-index-llms-groq==0.1.4"

# Layer 5 — Embeddings (largest layer — huggingface downloads)
RUN pip install --no-cache-dir --timeout 180 --retries 5 \
    "huggingface_hub==0.36.2" \
    "sentence-transformers==3.0.1" \
    "llama-index-embeddings-huggingface==0.2.2"

# Layer 6 — MCP (install last so sse-starlette pin is already in place)
RUN pip install --no-cache-dir --timeout 120 --retries 5 \
    "httpx>=0.24.0,<0.28.0" \
    "mcp>=1.10,<2" \
    "slowapi"

# Pre-bake the sentence transformer model into the image
ENV SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence_transformers
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

COPY . .

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]  