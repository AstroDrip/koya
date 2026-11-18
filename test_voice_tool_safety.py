import httpx
from app.config import MRCARE_TOOL_SECRET

BASE = "http://127.0.0.1:8001"
URL = f"{BASE}/api/voice-tool"

if not MRCARE_TOOL_SECRET:
    raise SystemExit("STOP: Webhook secret is not loaded.")

headers = {"X-MRCARE-Tool-Key": MRCARE_TOOL_SECRET}

# Test 1: Authenticated, read-only product lookup
r1 = httpx.post(
    URL,
    headers=headers,
    json={
        "action": "lookup_product",
        "model_number": "DEMO-AF-01"
    },
    timeout=15
)
print("1. Product lookup:", r1.status_code, r1.json())

# Test 2: Request without the secret must be rejected
r2 = httpx.post(
    URL,
    json={
        "action": "lookup_product",
        "model_number": "DEMO-AF-01"
    },
    timeout=15
)
print("2. Missing authentication:", r2.status_code)

# Test 3: Unconfirmed callback request must not be recorded
r3 = httpx.post(
    URL,
    headers=headers,
    json={
        "action": "request_callback",
        "customer_name": "Safety Test Customer",
        "phone": "9000000001",
        "customer_confirmed": False,
        "interaction_id": "safety-check-unconfirmed-001"
    },
    timeout=15
)
print("3. Unconfirmed callback rejected:", r3.status_code, r3.json())

# Test 4: Confirmed flag without interaction ID must be rejected
r4 = httpx.post(
    URL,
    headers=headers,
    json={
        "action": "request_callback",
        "customer_name": "Safety Test Customer",
        "phone": "9000000001",
        "customer_confirmed": True
    },
    timeout=15
)
print("4. Missing interaction ID rejected:", r4.status_code, r4.json())
