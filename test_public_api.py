import httpx
from app.config import MRCARE_TOOL_SECRET

BASE = "http://127.0.0.1:8002"
HEADERS = {"X-MRCARE-Tool-Key": MRCARE_TOOL_SECRET}

if not MRCARE_TOOL_SECRET:
    raise SystemExit("STOP: MRCARE_TOOL_SECRET is missing from .env")

# Test 1: Health check
r = httpx.get(f"{BASE}/healthz", timeout=10)
print("1. Health check:", r.status_code, r.json())

# Test 2: Authenticated product lookup
r = httpx.post(
    f"{BASE}/api/voice-lookup",
    headers=HEADERS,
    json={
        "action": "lookup_product",
        "model_number": "DEMO-AF-01"
    },
    timeout=15
)
print("2. Product lookup:", r.status_code, r.json())

# Test 3: Request without authentication
r = httpx.post(
    f"{BASE}/api/voice-lookup",
    json={
        "action": "lookup_product",
        "model_number": "DEMO-AF-01"
    },
    timeout=15
)
print("3. Missing authentication:", r.status_code)

# Test 4: Approved-price lookup
r = httpx.post(
    f"{BASE}/api/voice-lookup",
    headers=HEADERS,
    json={
        "action": "lookup_approved_price",
        "product_name": "Demo Air Fryer",
        "issue_code": "water_damage"
    },
    timeout=15
)
print("4. Price lookup:", r.status_code, r.json())

# Test 5: Write action must not be accepted by the read-only API
r = httpx.post(
    f"{BASE}/api/voice-lookup",
    headers=HEADERS,
    json={
        "action": "request_callback",
        "customer_name": "Safety Test Customer",
        "phone": "9000000001"
    },
    timeout=15
)
print("5. Callback action rejected:", r.status_code, r.text)
