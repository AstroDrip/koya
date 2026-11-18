from pydantic import ValidationError
from app.public_write_api import VoiceWritePayload

base = {
    "action": "request_callback",
    "customer_name": "Demo Test Customer",
    "phone": "701272727",
    "product_name": "Demo Air Fryer",
    "issue_description": "Test issue: air fryer is not heating.",
    "requested_date": "tomorrow",
    "requested_time": "11 AM",
    "customer_confirmed": False,
    "confirmation_text": "Demo callback request for testing validation only.",
    "interaction_id": "PHONE-VALIDATION-TEST-001"
}

try:
    VoiceWritePayload.model_validate(base)
    raise SystemExit("FAIL: Nine-digit number was accepted.")
except ValidationError:
    print("1. Nine-digit number rejected: PASSED")

valid = {**base, "phone": "+91 98765-43210"}
payload = VoiceWritePayload.model_validate(valid)
assert payload.phone == "9876543210"
print("2. +91 number normalized: PASSED")

valid = {**base, "phone": "9876543210"}
payload = VoiceWritePayload.model_validate(valid)
assert payload.phone == "9876543210"
print("3. Ten-digit number accepted: PASSED")
