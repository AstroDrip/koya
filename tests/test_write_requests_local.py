"""Isolated tests for callback, complaint and appointment requests via app.public_write_api.

Run with the project's Python environment:
  .\\.venv\\Scripts\\python.exe .\\tests\\test_write_requests_local.py

Uses a temporary SQLite file and a test-only secret. It never touches the demo DB
configured in .env, so no fictional pending work is added to the real queue.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

# Set these before importing the app: config reads secrets at import time.
tmp_dir = Path(tempfile.mkdtemp(prefix="mrcare-write-test-"))
test_db = tmp_dir / "write-test.sqlite3"
test_secret = "TEST_ONLY_" + uuid4().hex + uuid4().hex
os.environ["MRCARE_DB_PATH"] = str(test_db)
os.environ["MRCARE_WRITE_TOOL_SECRET"] = test_secret

from fastapi.testclient import TestClient  # noqa: E402
from app.public_write_api import app  # noqa: E402

URL = "/api/voice-request"
HEADERS = {"X-MRCARE-Write-Key": test_secret}
passed = 0


@contextmanager
def opened(path):
    """sqlite3 connection that is committed AND closed (needed for cleanup on Windows)."""
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def check(label, condition, detail=""):
    global passed
    if not condition:
        raise AssertionError(f"FAILED: {label} {detail}")
    passed += 1
    print(f"PASS: {label}")


def payload(action, **overrides):
    data = {
        "action": action,
        "customer_name": "TEST ONLY - Write Workflow Customer",
        "phone": "9000000001",
        "product_name": "LG Washing Machine",
        "serial_number": "DEMO-SERIAL-WM-001",
        "issue_description": "TEST ONLY - simulated fault",
        "requested_date": "tomorrow",
        "requested_time": "5 PM",
        "customer_confirmed": True,
        "confirmation_text": "Customer explicitly confirmed this test request after hearing the summary.",
        "interaction_id": "LOCALTEST-" + uuid4().hex,
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not ...}


def request_count():
    with opened(test_db) as conn:
        return conn.execute("SELECT COUNT(*) FROM service_requests").fetchone()[0]


try:
    with TestClient(app) as client:
        # --- Creation of each request type, always pending.
        created = {}
        for action, request_type in (("request_callback", "callback"), ("register_complaint", "complaint"),
                                     ("request_appointment", "appointment")):
            body = payload(action)
            r = client.post(URL, headers=HEADERS, json=body)
            result = r.json()
            check(f"{request_type} is created", r.status_code == 200 and result.get("created") is True
                  and result.get("duplicate") is False, r.text)
            check(f"{request_type} status is pending",
                  result["request"]["status"] == "pending" and result["request"]["request_type"] == request_type)
            check(f"{request_type} response warns nothing is booked or assigned",
                  "does not assign a technician" in result.get("warning", ""))
            created[action] = (body, result["request"]["id"])
        check("Three requests stored", request_count() == 3)

        with opened(test_db) as conn:
            rows = conn.execute("SELECT request_type, status, confirmation_text, product_id FROM service_requests").fetchall()
        check("Every stored request is pending", all(r[1] == "pending" for r in rows))
        check("Confirmation summary is stored verbatim",
              all(r[2].startswith("Customer explicitly confirmed") for r in rows))

        # --- Idempotent retries.
        for action, (body, request_id) in created.items():
            r = client.post(URL, headers=HEADERS, json=body)
            check(f"Retry of {action} is idempotent",
                  r.status_code == 200 and r.json()["duplicate"] is True and r.json()["request"]["id"] == request_id, r.text)
        check("Retries created no extra rows", request_count() == 3)

        # --- Authentication.
        r = client.post(URL, json=payload("request_callback"))
        check("Missing authentication -> 401", r.status_code == 401)
        r = client.post(URL, headers={"X-MRCARE-Write-Key": "wrong-" + uuid4().hex}, json=payload("request_callback"))
        check("Wrong key -> 401", r.status_code == 401)

        # --- Confirmation enforcement.
        for action in ("request_callback", "register_complaint", "request_appointment"):
            r = client.post(URL, headers=HEADERS, json=payload(action, customer_confirmed=False))
            check(f"Unconfirmed {action} -> 409", r.status_code == 409, r.text)
        r = client.post(URL, headers=HEADERS, json=payload("request_callback", confirmation_text=...))
        check("Missing confirmation summary -> 422", r.status_code == 422)

        # --- Required context.
        r = client.post(URL, headers=HEADERS, json=payload("request_callback", interaction_id=...))
        check("Missing interaction ID -> 422", r.status_code == 422)
        for action in ("request_callback", "request_appointment"):
            r = client.post(URL, headers=HEADERS, json=payload(action, requested_date=..., requested_time=...))
            check(f"{action} without preferred date/time -> 422", r.status_code == 422)
        r = client.post(URL, headers=HEADERS, json=payload("register_complaint", requested_date=..., requested_time=...))
        check("Complaint without date/time is accepted", r.status_code == 200 and r.json()["request"]["status"] == "pending")

        # --- Phone-number validation.
        for bad in ("701272727", "1234567890", "98765432101", "abcdefghij", "", "   "):
            r = client.post(URL, headers=HEADERS, json=payload("request_callback", phone=bad))
            check(f"Invalid phone {bad!r} -> 422", r.status_code == 422, r.text)
        r = client.post(URL, headers=HEADERS, json=payload("request_callback", phone=...))
        check("Missing phone -> 422", r.status_code == 422)
        before = request_count()
        r = client.post(URL, headers=HEADERS, json=payload("request_callback", phone="9876543210"))
        check("Valid 10-digit Indian mobile accepted", r.status_code == 200, r.text)
        r = client.post(URL, headers=HEADERS, json=payload("request_callback", phone="+91 98765-43210"))
        check("+91 formatted mobile accepted", r.status_code == 200, r.text)
        with opened(test_db) as conn:
            phones = [row[0] for row in conn.execute("SELECT phone FROM service_requests ORDER BY id DESC LIMIT 2")]
        check("Phone is stored normalised to 10 digits", phones == ["9876543210", "9876543210"], str(phones))
        check("Exactly two rows were added by the valid phone tests", request_count() == before + 2)

        # --- Other invalid input.
        before = request_count()
        invalid_cases = {
            "Unknown action": payload("delete_everything"),
            "Read action on the write API": payload("lookup_product"),
            "Unexpected extra field": payload("request_callback", status="confirmed"),
            "Blank customer name": payload("request_callback", customer_name="   "),
            "Missing product name": payload("request_callback", product_name=...),
            "Blank issue description": payload("register_complaint", issue_description=" "),
            "Too-short confirmation summary": payload("request_callback", confirmation_text="ok"),
        }
        for label, body in invalid_cases.items():
            r = client.post(URL, headers=HEADERS, json=body)
            check(f"{label} -> 422", r.status_code == 422, r.text)
        check("Rejected requests stored nothing", request_count() == before)

        with opened(test_db) as conn:
            statuses = {row[0] for row in conn.execute("SELECT DISTINCT status FROM service_requests")}
        check("No status other than pending exists", statuses == {"pending"})

    print(f"ALL WRITE REQUEST TESTS PASSED ({passed} checks)")
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)
