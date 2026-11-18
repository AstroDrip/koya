import httpx
from app.config import MRCARE_WRITE_TOOL_SECRET

BASE = "https://gordon-notification-engine-sweet.trycloudflare.com"
URL = f"{BASE}/api/voice-request"
SECRET = MRCARE_WRITE_TOOL_SECRET

if not SECRET or len(SECRET) < 32:
    raise SystemExit("STOP: Write secret is missing or too short.")

payload = {
    "action": "request_callback",
    "customer_name": "Safety Test Customer",
    "phone": "9000000001",
    "product_name": "Demo Air Fryer",
    "issue_description": "Safety test only.",
    "confirmation_text": "The customer confirmed this test summary.",
    "interaction_id": "public-safety-test-001",
    "requested_date": "tomorrow",
    "requested_time": "11 AM",
    "customer_confirmed": True
}

def test(label, response, expected):
    print(f"{label}: HTTP {response.status_code}; expected {expected}")
    if response.status_code not in expected:
        print("Unexpected response:", response.text[:500])

try:
    health = httpx.get(f"{BASE}/healthz", timeout=25)
    test("1. Public health", health, {200})

    # No authentication
    r = httpx.post(URL, json=payload, timeout=25)
    test("2. Missing authentication", r, {401})

    headers = {"X-MRCARE-Write-Key": SECRET}

    # Customer confirmation is false
    r = httpx.post(
        URL,
        headers=headers,
        json={**payload, "customer_confirmed": False},
        timeout=25
    )
    test("3. Unconfirmed request", r, {409})

    # Missing interaction ID
    missing_id = {k: v for k, v in payload.items() if k != "interaction_id"}
    r = httpx.post(URL, headers=headers, json=missing_id, timeout=25)
    test("4. Missing interaction ID", r, {422})

    # Missing requested date/time
    no_time = {
        **payload,
        "requested_date": None,
        "requested_time": None
    }
    r = httpx.post(URL, headers=headers, json=no_time, timeout=25)
    test("5. Missing preferred date/time", r, {422})

except httpx.HTTPError as exc:
    print("Connection failed:", type(exc).__name__, str(exc))

