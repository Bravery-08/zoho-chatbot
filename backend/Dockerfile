FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app/ ./app/
COPY data/ ./data/
COPY ingest.py .
COPY .env .

# Run ingestion then start server
CMD ["sh", "-c", "python ingest.py && uvicorn app.main:app --host 0.0.0.0 --port 8000"]