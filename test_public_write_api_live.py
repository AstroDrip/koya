import httpx
from app.config import MRCARE_WRITE_TOOL_SECRET

BASE = "http://127.0.0.1:8003"
URL = f"{BASE}/api/voice-request"
SECRET = MRCARE_WRITE_TOOL_SECRET

if not SECRET or len(SECRET) < 32:
    raise SystemExit("STOP: Write secret is missing or too short.")

base_payload = {
    "action": "request_callback",
    "customer_name": "Safety Test Customer",
    "phone": "9000000001",
    "product_name": "Demo Air Fryer",
    "issue_description": "Testing only; no real service request.",
    "confirmation_text": "The customer confirmed this test summary.",
    "interaction_id": "safety-live-test-001",
    "requested_date": "tomorrow",
    "requested_time": "11 AM",
    "customer_confirmed": True
}

def show(label, response, expected):
    print(f"{label}: HTTP {response.status_code}; expected {expected}")
    if response.status_code not in expected:
        print("Unexpected response:", response.text[:500])

# 1. No authentication: must be rejected.
r = httpx.post(URL, json=base_payload, timeout=10)
show("1. Missing authentication", r, {401})

# 2. Authenticated, but customer has not confirmed.
payload = {**base_payload, "customer_confirmed": False}
r = httpx.post(
    URL,
    headers={"X-MRCARE-Write-Key": SECRET},
    json=payload,
    timeout=10
)
show("2. Unconfirmed request", r, {409})

# 3. Authenticated and confirmed, but missing interaction ID.
payload = {k: v for k, v in base_payload.items() if k != "interaction_id"}
r = httpx.post(
    URL,
    headers={"X-MRCARE-Write-Key": SECRET},
    json=payload,
    timeout=10
)
show("3. Missing interaction ID", r, {422})

# 4. Authenticated and confirmed, but no customer-stated time.
payload = {
    **base_payload,
    "requested_date": None,
    "requested_time": None
}
r = httpx.post(
    URL,
    headers={"X-MRCARE-Write-Key": SECRET},
    json=payload,
    timeout=10
)
show("4. Missing preferred date/time", r, {422})

print("Safety checks complete. No request should have been created.")
