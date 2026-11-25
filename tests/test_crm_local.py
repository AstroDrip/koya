"""Exercise CRM routes and concurrency with a temporary database and fake OTP delivery."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


# Use only isolated data and fake providers even when the shell has deployment settings.
os.environ.update(MRCARE_DATABASE_URL="", MRCARE_REDIS_URL="", MRCARE_RECORDINGS_BUCKET="",
                  MRCARE_WORKER_ALLOW_EXTERNAL="0", MRCARE_WHATSAPP_SEND_ENABLED="0",
                  MRCARE_SARVAM_OUTBOUND_ENABLED="0")

os.environ["MRCARE_CRM_OTP_SECRET"] = "synthetic-tests-only-verification-key"

from app import crm, crm_api, crm_identity
from app.admin_cli import migrate_with_backup
from app.database import SCHEMA, connection, database_ready, now_iso
from app.main import app
from app.staff_auth import issue_owner_session

ROOT = "/api/crm"


def submit(client, headers, route, payload):
    response = client.post(ROOT + route, headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def owner_and_cases(client):
    headers = {"Authorization": f"Bearer {issue_owner_session('crm-owner')}"}
    first = {"name": "Synthetic Customer", "operation_key": "create-first"}
    customer = submit(client, headers, "/customers", first)
    assert submit(client, headers, "/customers", first) == customer
    assert client.post(ROOT + "/customers", headers=headers, json={**first, "name": "Other"}).status_code == 409
    another = submit(client, headers, "/customers", {"name": "Other Fixture", "operation_key": "create-other"})
    assert client.get(ROOT + f"/customers/{customer['id']}").status_code == 401

    with connection() as conn:
        assets = [
            conn.execute("INSERT INTO products(product_name,serial_number,created_at) VALUES (?,?,?)",
                         ("Synthetic asset", serial, now_iso())).lastrowid
            for serial in ("CRM-A", "CRM-B")
        ]
    for asset, serial in zip(assets, ("CRM-A", "CRM-B")):
        route = ROOT + f"/customers/{customer['id']}/assets/{asset}"
        assert client.put(route, headers=headers, json={"serial": serial}).status_code == 204
    assert client.put(ROOT + f"/customers/{another['id']}/assets/{assets[0]}", headers=headers, json={"serial": "CRM-A"}).status_code == 409
    detail = client.get(ROOT + f"/customers/{customer['id']}", headers=headers).json()
    assert [asset["id"] for asset in detail["assets"]] == assets

    payload = {
        "customer_id": customer["id"], "product_id": assets[0], "channel": "voice",
        "description": "Synthetic issue A", "customer_confirmed": True,
        "confirmation_text": "Explicitly confirmed synthetic complaint", "operation_key": "case-one",
    }
    assert client.post(ROOT + "/cases", headers=headers, json={**payload, "customer_confirmed": False}).status_code == 409
    assert client.post(ROOT + "/cases", headers=headers, json={**payload, "customer_id": another["id"]}).status_code == 422
    first_case = submit(client, headers, "/cases", payload)
    assert submit(client, headers, "/cases", payload) == first_case
    assert client.post(ROOT + "/cases", headers=headers, json={**payload, "description": "Changed"}).status_code == 409
    second_case = submit(client, headers, "/cases", {**payload, "description": "Synthetic issue B", "operation_key": "case-two"})
    assert first_case["reference"] != second_case["reference"]

    case_path = f"/cases/{first_case['id']}"
    transition = {"expected_version": 1, "status": "accepted", "reason": "Confirmed for dispatch review"}
    assert client.post(ROOT + case_path + "/transitions", json=transition).status_code == 401
    assert submit(client, headers, case_path + "/transitions", transition)["status"] == "accepted"
    assert submit(client, headers, "/cases", payload) == first_case
    assert client.post(ROOT + case_path + "/transitions", headers=headers, json=transition).status_code == 409
    assert client.post(ROOT + case_path + "/transitions", headers=headers, json={**transition, "expected_version": 2, "status": "rejected"}).status_code == 409
    voice = {"channel": "voice", "provider": "fake", "provider_account": "one",
             "external_id": "session", "summary": "Voice intake", "facts": {"model": "test"}}
    whatsapp = {**voice, "channel": "whatsapp", "summary": "WhatsApp follow-up"}
    interaction = submit(client, headers, case_path + "/interactions", voice)
    assert submit(client, headers, case_path + "/interactions", voice) == interaction
    assert client.post(ROOT + f"/cases/{second_case['id']}/interactions", headers=headers, json=voice).status_code == 409
    submit(client, headers, case_path + "/interactions", whatsapp)
    case = client.get(ROOT + case_path, headers=headers).json()
    assert [(entry["previous_status"], entry["status"]) for entry in case["history"]] == [(None, "pending"), ("pending", "accepted")]
    assert all(entry["actor_id"] for entry in case["history"])
    try:
        with connection() as conn:
            conn.execute("DELETE FROM crm_case_history WHERE case_id=?", (first_case["id"],))
    except sqlite3.IntegrityError as exc:
        assert "append-only" in str(exc)
    else:
        raise AssertionError("Case history must be append-only")
    assert [entry["channel"] for entry in case["interactions"]] == ["voice", "whatsapp"]
    assert client.get(ROOT + f"/cases/{second_case['id']}", headers=headers).json()["interactions"] == []
    return headers, customer, another, first_case, second_case, payload


def verification(client, headers, customer, another, first_case, second_case):
    deliveries = []
    phone = {"mobile": "+91 98765-43210"}
    enroll_path = f"/customers/{customer['id']}/contact-challenges"
    assert client.post(ROOT + enroll_path, headers=headers, json=phone).status_code == 503
    app.dependency_overrides[crm_api.deliver_otp] = lambda: lambda mobile, code: deliveries.append((mobile, code))
    enrolled = submit(client, headers, enroll_path, phone)
    assert deliveries[-1][0] == "9876543210"
    otp = deliveries[-1][1]
    code = {**enrolled, "code": otp}
    assert submit(client, headers, "/contact-challenges/confirm", code)["verified"] is True
    assert client.post(ROOT + "/contact-challenges/confirm", headers=headers, json=code).status_code == 404
    assert submit(client, headers, "/customers/resolve", {**phone, "name": customer["name"]})["customer_id"] == customer["id"]
    assert submit(client, headers, "/customers/resolve", {**phone, "name": another["name"]}) == {"status": "review_required"}
    assert submit(client, headers, "/customers/resolve", {**phone, "name": customer["name"]})["customer_id"] == customer["id"]
    assert [item["name"] for item in client.get(ROOT + "/customers", headers=headers).json()["items"]].count(customer["name"]) == 1

    case_token_path = f"/cases/{first_case['id']}/token"
    token = submit(client, headers, case_token_path, None)["token"]
    alternate_token = submit(client, headers, f"/cases/{second_case['id']}/token", None)["token"]
    assert len(token) == 43 and token != alternate_token
    resume = {"reference": first_case["reference"], "token": token, **phone}
    other_case = {**resume, "reference": second_case["reference"]}
    assert client.post(ROOT + "/resume/challenge", json=other_case).status_code == 404
    assert client.post(ROOT + "/resume/challenge", json={**resume, "mobile": "9876543211"}).status_code == 404
    malformed = client.post(ROOT + "/resume/challenge", json={**resume, "token": "do-not-echo-me"})
    assert malformed.status_code == 422 and "do-not-echo-me" not in malformed.text
    started = submit(client, {}, "/resume/challenge", resume)
    otp = deliveries[-1][1]
    confirm = {key: value for key, value in resume.items() if key != "mobile"}
    confirm.update(started, code=otp)
    assert client.post(ROOT + "/resume/confirm", json={**confirm, "reference": second_case["reference"], "token": alternate_token}).status_code == 404
    result = submit(client, {}, "/resume/confirm", confirm)
    assert result["reference"] == first_case["reference"] and result["description"] == "Synthetic issue A"
    assert "customer_id" not in result
    assert client.post(ROOT + "/resume/confirm", json=confirm).status_code == 404
    started = submit(client, {}, "/resume/challenge", resume)
    otp = deliveries[-1][1]
    confirm.update(started, code=otp)
    incorrect = "00000000" if otp != "00000000" else "11111111"
    for _ in range(5):
        assert client.post(ROOT + "/resume/confirm", json={**confirm, "code": incorrect}).status_code == 404
    assert client.post(ROOT + "/resume/confirm", json=confirm).status_code == 404
    assert client.post(ROOT + "/resume/challenge", json=resume).status_code == 429
    assert client.delete(ROOT + case_token_path, headers=headers).status_code == 204
    assert client.post(ROOT + "/resume/challenge", json=resume).status_code == 404
    with connection() as conn:
        hashes = [row[0] for row in conn.execute("SELECT verifier FROM crm_case_tokens")]
        assert token not in hashes and alternate_token not in hashes
        assert conn.execute("SELECT count(*) FROM crm_identity_audit WHERE event='resume_verification_failed'").fetchone()[0] == 5
        conn.execute("UPDATE crm_case_tokens SET expires_at=1 WHERE case_id=?", (second_case["id"],))
    assert client.post(ROOT + "/resume/challenge", json={**other_case, "token": alternate_token}).status_code == 404


def concurrent_cases(payload):
    with connection(read_only=True) as conn:
        actor_id = conn.execute("SELECT id FROM staff_users WHERE username='crm-owner'").fetchone()[0]

    class KolkataMidnight:
        @staticmethod
        def now(zone):
            return datetime(2030, 1, 1, 18, 30, tzinfo=timezone.utc).astimezone(zone)

    data = crm_api.CaseCreate(**payload).model_dump()
    with patch.object(crm, "datetime", KolkataMidnight):
        with ThreadPoolExecutor(max_workers=50) as pool:
            cases = list(pool.map(lambda n: crm.create_case({**data, "operation_key": f"parallel-{n}"}, actor_id), range(50)))
    assert {case["reference"] for case in cases} == {f"020130{seq:03d}" for seq in range(1, 51)}
    target = cases[0]

    def accept(_):
        try:
            crm.transition_case(target["id"], 1, "accepted", "Concurrent review", actor_id)
            return True
        except crm.CRMError as exc:
            assert exc.status == 409
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(accept, range(8))) == 1
    assert len(crm.case_detail(target["id"])["history"]) == 2


def upgrade_from_phase_one(directory):
    path = directory / "phase-one.sqlite3"
    backup = directory / "phase-one-backup.sqlite3"
    os.environ["MRCARE_DB_PATH"] = str(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(SCHEMA + """
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (1,'fixture'),(2,'fixture');
            CREATE TABLE staff_users (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL CHECK(role = 'owner')
            );
            CREATE TABLE staff_sessions (
                token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX idx_staff_sessions_expiry ON staff_sessions(expires_at);
            INSERT INTO customers (id,name,phone,created_at) VALUES (9,'Synthetic legacy contact','900000000','fixture');
            INSERT INTO service_requests (request_type,customer_id,phone,created_at) VALUES ('callback',9,'900000000','fixture');
            INSERT INTO staff_users VALUES (1,'legacy-owner','owner');
            INSERT INTO staff_sessions VALUES ('synthetic-verifier',1,2000000000);
        """)
    assert not database_ready()
    migrate_with_backup(backup)
    assert database_ready()
    with closing(sqlite3.connect(path)) as current, closing(sqlite3.connect(backup)) as previous:
        for table in ("customers", "products", "approved_prices", "service_requests", "staff_users", "staff_sessions"):
            assert current.execute(f"SELECT * FROM {table}").fetchall() == previous.execute(f"SELECT * FROM {table}").fetchall()
        assert current.execute("SELECT count(*) FROM crm_contacts").fetchone() == (0,)
        assert current.execute("PRAGMA foreign_key_check").fetchall() == []
        assert previous.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]


def main():
    with TemporaryDirectory() as folder:
        upgrade_from_phase_one(Path(folder))
        os.environ["MRCARE_DB_PATH"] = str(Path(folder) / "local-crm.sqlite3")
        try:
            with TestClient(app) as client:
                headers, customer, another, first_case, second_case, payload = owner_and_cases(client)
                with patch.object(crm_identity, "time", SimpleNamespace(time=lambda: 2_000_000_000)):
                    verification(client, headers, customer, another, first_case, second_case)
                concurrent_cases(payload)
        finally:
            app.dependency_overrides.clear()
    print("CRM checks passed (identity, cross-channel case isolation, audit, OTP controls, and 50 concurrent cases).")


if __name__ == "__main__":
    main()
