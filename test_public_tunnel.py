import httpx
from app.config import MRCARE_TOOL_SECRET

BASE = "https://fabric-cold-interventions-minds.trycloudflare.com"
URL = f"{BASE}/api/voice-lookup"
HEADERS = {"X-MRCARE-Tool-Key": MRCARE_TOOL_SECRET}

if not MRCARE_TOOL_SECRET:
    raise SystemExit("STOP: Webhook secret is missing.")

if (
    MRCARE_TOOL_SECRET.lower().startswith("replace_with")
    or "your_secret" in MRCARE_TOOL_SECRET.lower()
):
    raise SystemExit("STOP: Placeholder webhook secret detected.")

def check(label, method, url, **kwargs):
    try:
        response = httpx.request(
            method, url, timeout=25, **kwargs
        )
        try:
            body = response.json()
        except ValueError:
            body = response.text[:300]
        print(f"{label}: HTTP {response.status_code}")
        print(body)
    except httpx.HTTPError as exc:
        print(f"{label}: CONNECTION FAILED: {type(exc).__name__}: {exc}")

check("1. Public health", "GET", f"{BASE}/healthz")

check(
    "2. Authenticated product lookup",
    "POST",
    URL,
    headers=HEADERS,
    json={
        "action": "lookup_product",
        "model_number": "DEMO-AF-01"
    }
)

check(
    "3. Unauthenticated request",
    "POST",
    URL,
    json={
        "action": "lookup_product",
        "model_number": "DEMO-AF-01"
    }
)

check(
    "4. Public approved-price lookup",
    "POST",
    URL,
    headers=HEADERS,
    json={
        "action": "lookup_approved_price",
        "product_name": "Demo Air Fryer",
        "issue_code": "water_damage"
    }
)

check(
    "5. Write action blocked",
    "POST",
    URL,
    headers=HEADERS,
    json={
        "action": "request_callback",
        "customer_name": "Safety Test Customer",
        "phone": "9000000001"
    }
)

