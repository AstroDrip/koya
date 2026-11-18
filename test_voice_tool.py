import httpx
from app.config import MRCARE_TOOL_SECRET

if not MRCARE_TOOL_SECRET:
    raise SystemExit("Webhook secret missing. Check your .env file.")

url = "http://127.0.0.1:8001/api/voice-tool"

response = httpx.post(
    url,
    headers={"X-MRCARE-Tool-Key": MRCARE_TOOL_SECRET},
    json={
        "action": "lookup_product",
        "model_number": "DEMO-AF-01"
    },
    timeout=15
)

print("Authenticated request status:", response.status_code)
print("Response:", response.json())

auth_test = httpx.post(
    url,
    json={
        "action": "lookup_product",
        "model_number": "DEMO-AF-01"
    },
    timeout=15
)

print("Unauthenticated request status:", auth_test.status_code)
