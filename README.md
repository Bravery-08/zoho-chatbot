# WhatsApp RAG Bot

A WhatsApp chatbot that answers queries by retrieving answers from your internal SOP documents using Retrieval-Augmented Generation (RAG). Built with Baileys, FastAPI, LlamaIndex, ChromaDB, and Groq.

---

## Architecture

```
WhatsApp User
     │
     ▼
Baileys (Node.js)          ← Connects to WhatsApp Web
     │  HTTP POST /query
     ▼
FastAPI (Python)           ← RAG query engine
     │
     ├── ChromaDB           ← Vector store (local, persistent)
     │       └── all-MiniLM-L6-v2 embeddings
     │
     └── Groq API           ← Llama 3.3 70B inference
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| WhatsApp Integration | [Baileys (WhiskeySockets)](https://github.com/WhiskeySockets/Baileys) |
| LLM | [Groq](https://console.groq.com) + Llama 3.3 70B |
| Vector Database | [ChromaDB](https://www.trychroma.com/) (embedded mode) |
| Embeddings | all-MiniLM-L6-v2 (local, via sentence-transformers) |
| RAG Framework | [LlamaIndex](https://www.llamaindex.ai/) |
| Backend | [FastAPI](https://fastapi.tiangolo.com/) |
| Local Tunnel | [Ngrok](https://ngrok.com/) |

---

## Project Structure

```
whatsapp-rag-bot/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── config.py         # Environment variables and constants
│   │   ├── rag.py            # LlamaIndex + ChromaDB + Groq setup
│   │   └── main.py           # FastAPI routes and rate limiting
│   ├── data/
│   │   └── sops.txt          # Your SOP document(s)
│   ├── chroma_store/         # ChromaDB vector store (auto-generated)
│   ├── ingest.py             # One-time document ingestion script
│   ├── verify_store.py       # Sanity check ChromaDB contents
│   ├── healthcheck.py        # Verify all services are running
│   ├── test_query.py         # Test RAG pipeline directly
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env
│
├── bot/
│   ├── src/
│   │   ├── index.js          # Baileys connection and event loop
│   │   ├── handler.js        # Message extraction and history
│   │   └── api.js            # FastAPI HTTP client
│   ├── auth_info/            # WhatsApp session (auto-generated)
│   ├── package.json
│   ├── Dockerfile
│   └── .env
│
├── docker-compose.yml
├── start.bat                 # Windows one-click launcher
├── .gitignore
└── README.md
```

---

## Prerequisites

- Python 3.11+
- Node.js 20+
- [Groq API key](https://console.groq.com) (free tier available)
- [Ngrok account](https://ngrok.com) (free tier available)
- A WhatsApp account to link as the bot

---

## Setup

### 1. Clone the Repository

```bash
git clone https://github.com/yourname/whatsapp-rag-bot.git
cd whatsapp-rag-bot
```

### 2. Backend Setup

```bash
cd backend
python -m venv venv

# Windows
venv\Scripts\activate

# Mac/Linux
source venv/bin/activate

pip install -r requirements.txt
```

Create `backend/.env` from the example:

```bash
cp .env.example .env
```

Fill in your values:

```env
GROQ_API_KEY=your_groq_api_key_here
CHROMA_PERSIST_DIR=./chroma_store
DATA_DIR=./data
TOP_K=5
ANONYMIZED_TELEMETRY=False
```

### 3. Bot Setup

```bash
cd bot
npm install
```

Create `bot/.env`:

```env
FASTAPI_URL=http://localhost:8000
```

### 4. Ngrok Setup

```bash
ngrok config add-authtoken your_ngrok_authtoken_here
```

### 5. Add Your SOP Document

Place your SOP text file at:

```
backend/data/sops.txt
```

### 6. Ingest Documents into ChromaDB

```bash
cd backend
venv\Scripts\activate
python ingest.py
```

Verify ingestion:

```bash
python verify_store.py
```

Expected output:
```
Total chunks in ChromaDB: 13
Sample chunk: ...
```

---

## Running the Bot

### Option A — Windows (One Command)

```bash
.\start.bat
```

This opens three terminal windows automatically:
- **FastAPI Backend** on port 8000
- **Ngrok Tunnel** forwarding to port 8000
- **WhatsApp Bot** ready for QR scan

### Option B — Manual (Three Terminals)

**Terminal 1 — Backend:**
```bash
cd backend
venv\Scripts\activate
uvicorn app.main:app --port 8000
```

**Terminal 2 — Ngrok:**
```bash
ngrok http 8000
```

**Terminal 3 — Bot:**
```bash
cd bot
npm start
```

### Scan QR Code

On first run the bot terminal shows a QR code. On your phone:

**WhatsApp → ⋮ Menu → Linked Devices → Link a Device → Scan QR**

After scanning:
```
✅ WhatsApp connected successfully!
Bot is ready to receive messages.
```

The session is saved to `bot/auth_info/` — you won't need to scan again unless you log out.

---

## Docker

### Build and Run

```bash
docker compose up --build
```

### View WhatsApp QR Code (first run)

```bash
docker logs whatsapp-bot
```

### Stop All Services

```bash
docker compose down
```

---

## Usage

Send any WhatsApp message to the linked number. The bot retrieves the most relevant SOP sections and answers using Llama 3.3 70B.

**Example queries:**

```
How do I apply for sick leave?
What is the refund policy if I cancel after 5 days?
Who do I escalate a critical incident to?
What is the password policy?
How many earned leave days do I get?
```

**Out-of-scope queries** (bot responds with a fallback):
```
What is the capital of France?    →  "I don't have information about that in our SOPs."
```

**Reset conversation history:**
```
/reset    →  "Conversation history cleared. Starting fresh!"
```

---

## API Reference

### `GET /health`

Check if the server and RAG engine are ready.

```json
{
  "status": "ok",
  "engine_loaded": true
}
```

### `POST /query`

Submit a query to the RAG pipeline.

**Request:**
```json
{
  "message": "How do I apply for sick leave?",
  "sender": "+91XXXXXXXXXX",
  "history": [
    { "role": "user",      "content": "Previous question" },
    { "role": "assistant", "content": "Previous answer"   }
  ]
}
```

**Response:**
```json
{
  "response": "To apply for sick leave, log into the HRMS portal...",
  "source_chunks": 3,
  "latency_ms": 1823
}
```

**Rate limit:** 10 requests per minute per IP.

**Interactive docs:** `http://localhost:8000/docs`

---

## Re-indexing SOPs

When your SOP document changes:

**Incremental (add new content):**
```bash
python ingest.py
```

**Full rebuild (wipe and re-index from scratch):**
```bash
python ingest.py --force
```

---

## Tuning Retrieval Quality

Adjust in `backend/.env`:

```env
TOP_K=5    # number of chunks retrieved per query
```

Adjust similarity threshold in `backend/app/rag.py`:

```python
node_postprocessors=[
    SimilarityPostprocessor(similarity_cutoff=0.6)
]
```

| Problem | Fix |
|---|---|
| Answer misses relevant info | Lower `similarity_cutoff` or raise `TOP_K` |
| Answer contains irrelevant info | Raise `similarity_cutoff` or lower `TOP_K` |
| Fallback triggers too often | Lower `similarity_cutoff` |
| Response too slow | Lower `TOP_K` |

---

## Troubleshooting

**Port 8000 already in use:**
```powershell
# Windows PowerShell (run as Administrator)
Get-NetTCPConnection -LocalPort 8000 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

**ChromaDB telemetry warnings:**
```
Failed to send telemetry event...
```
Add `ANONYMIZED_TELEMETRY=False` to `backend/.env`. Harmless if not set.

**WhatsApp session expired:**
```bash
# Delete session and re-scan QR
rm -rf bot/auth_info/
npm start
```

**Bot not responding to messages:**
1. Check FastAPI is running: `http://localhost:8000/health`
2. Check bot terminal for errors
3. Run healthcheck: `python backend/healthcheck.py`

**Groq API errors:**
- Verify `GROQ_API_KEY` in `backend/.env`
- Check quota at [console.groq.com](https://console.groq.com)

---

## Environment Variables

### `backend/.env`

| Variable | Description | Default |
|---|---|---|
| `GROQ_API_KEY` | Groq API key | required |
| `CHROMA_PERSIST_DIR` | ChromaDB storage path | `./chroma_store` |
| `DATA_DIR` | SOP documents directory | `./data` |
| `TOP_K` | Chunks retrieved per query | `5` |
| `ANONYMIZED_TELEMETRY` | Disable ChromaDB telemetry | `False` |

### `bot/.env`

| Variable | Description | Default |
|---|---|---|
| `FASTAPI_URL` | FastAPI backend URL | `http://localhost:8000` |

---

## Security Notes

- `bot/auth_info/` contains your WhatsApp session credentials — never commit this to git
- `backend/.env` and `bot/.env` contain API keys — never commit these
- Both directories are covered by `.gitignore`
- Rate limiting is set to 10 requests/minute — adjust in `main.py` if needed

---

## License

MIT