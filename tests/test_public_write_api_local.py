"""Self-contained local smoke test for app.public_write_api.

Run with the project's Python environment:
  .\\.venv\\Scripts\\python.exe .\\tests\\test_public_write_api_local.py

Uses a temporary SQLite file and a test-only secret. It never touches the demo DB
configured in .env and removes its temporary DB when it exits.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import sys
from pathlib import Path
from uuid import uuid4

# Ensure the project root is importable when this file is run directly.
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

# Set these before importing the app: config reads secrets at import time.
test_dir = Path(tempfile.gettempdir())
test_db = test_dir / f"mrcare-write-test-{uuid4().hex}.sqlite3"
test_secret = "TEST_ONLY_" + uuid4().hex + uuid4().hex
os.environ["MRCARE_DB_PATH"] = str(test_db)
os.environ["MRCARE_WRITE_TOOL_SECRET"] = test_secret

from fastapi.testclient import TestClient  # noqa: E402
from app.public_write_api import app  # noqa: E402


def assert_status(label, response, expected):
    print(f"{label}: HTTP {response.status_code}")
    if response.status_code != expected:
        print(response.text)
        raise AssertionError(f"{label}: expected {expected}, got {response.status_code}")


base_payload = {
    "action": "request_callback",
    "customer_name": "TEST ONLY - Write API Customer",
    "phone": "9000000001",
    "address": "TEST ONLY - Demo locality",
    "pincode": "673001",
    "product_name": "Demo Air Fryer",
    "model_number": "DEMO-AF-01",
    "issue_description": "TEST ONLY - simulated air fryer not heating",
    "requested_date": "tomorrow",
    "requested_time": "5 PM",
    "customer_confirmed": True,
    "confirmation_text": "Customer explicitly confirmed this test callback request after hearing the summary.",
    "interaction_id": "LOCALTEST-" + uuid4().hex,
}

try:
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert_status("1. Health check", r, 200)
        assert r.json().get("service") == "mrcare-confirmed-demo-write-api"

        r = client.post("/api/voice-request", json=base_payload)
        assert_status("2. Missing authentication", r, 401)

        r = client.post(
            "/api/voice-request",
            headers={"X-MRCARE-Write-Key": test_secret},
            json={**base_payload, "customer_confirmed": False},
        )
        assert_status("3. Unconfirmed request", r, 409)

        no_interaction = dict(base_payload)
        no_interaction.pop("interaction_id")
        r = client.post(
            "/api/voice-request",
            headers={"X-MRCARE-Write-Key": test_secret},
            json=no_interaction,
        )
        assert_status("4. Missing interaction ID", r, 422)

        no_time = dict(base_payload)
        no_time.pop("requested_date")
        no_time.pop("requested_time")
        r = client.post(
            "/api/voice-request",
            headers={"X-MRCARE-Write-Key": test_secret},
            json=no_time,
        )
        assert_status("5. Callback without preferred date/time", r, 422)

        headers = {"X-MRCARE-Write-Key": test_secret}
        r = client.post("/api/voice-request", headers=headers, json=base_payload)
        assert_status("6. Confirmed callback request", r, 200)
        result = r.json()
        assert result.get("created") is True
        assert result.get("request", {}).get("status") == "pending"
        assert "does not assign a technician" in result.get("warning", "")
        print("   Created pending request:", result["request"])

        # The same action + interaction ID is idempotent, not a second insert.
        r = client.post("/api/voice-request", headers=headers, json=base_payload)
        assert_status("7. Duplicate retry", r, 200)
        assert r.json().get("duplicate") is True

        with sqlite3.connect(test_db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM service_requests").fetchone()[0]
        print("8. Stored requests in isolated temporary DB:", count)
        assert count == 1, f"Expected exactly one request after retry; found {count}"

    print("ALL LOCAL WRITE API TESTS PASSED")
finally:
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(test_db) + suffix).unlink(missing_ok=True)
        except OSError:
            pass
