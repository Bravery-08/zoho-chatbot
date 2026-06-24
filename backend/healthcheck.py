import requests

services = [
    ("FastAPI",  "http://localhost:8000/health"),
    ("Ngrok",    "http://localhost:4040/api/tunnels"),  # Ngrok local dashboard
]

for name, url in services:
    try:
        r = requests.get(url, timeout=3)
        print(f"✅ {name} — OK ({r.status_code})")
    except Exception as e:
        print(f"❌ {name} — FAILED: {e}")