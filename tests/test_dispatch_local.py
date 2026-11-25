"""Exercise dispatch, scheduling, and technician isolation with synthetic SQLite data."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
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

from app import dispatch
from app.admin_cli import migrate_with_backup
from app.crm import KOLKATA
from app.database import SCHEMA, connection, database_ready
from app.crm_schema import CRM_SCHEMA
from app.main import app
from app.staff_auth import issue_owner_session

NOW = datetime(2026, 11, 19, 10, tzinfo=KOLKATA)
DISPATCH = "/api/dispatch"
TECH = "/api/technician"
CRM = "/api/crm"


def call(client, method, url, headers, payload=None, expected=200):
    response = client.request(method, url, headers=headers, json=payload)
    assert response.status_code == expected, (url, response.status_code, response.text)
    return response.json() if response.content else None


def interval(minutes, length=30, **extra):
    start = NOW + timedelta(minutes=minutes)
    return {"start": start.isoformat(), "end": (start + timedelta(minutes=length)).isoformat(),
            "reason": "Synthetic approved visit", **extra}


def run(client, clock):
    owner = {"Authorization": "Bearer " + issue_owner_session("dispatch-owner")}
    hours = [{"weekday": NOW.weekday(), "start_minute": 480, "end_minute": 1080}]
    profile = {"name": "Synthetic technician", "active": True, "capacity": 2,
               "pincodes": ["673001"], "skills": ["appliance"], "hours": hours}

    def technician(code, **override):
        row = call(client, "PUT", DISPATCH + "/technicians/" + code, owner, {**profile, **override})
        if not row["active"]:
            return row["id"], {}
        session = call(client, "POST", f"{DISPATCH}/technicians/{row['id']}/session", owner)["token"]
        return row["id"], {"Authorization": "Bearer " + session}

    a, first = technician("tech-a")
    b, second = technician("tech-b")
    technician("outside", pincodes=["400001"])
    technician("skill", skills=["plumbing"])
    technician("off-shift", hours=[])
    technician("inactive", active=False)
    leave, _ = technician("on-leave")
    call(client, "POST", f"{DISPATCH}/technicians/{leave}/leave", owner, interval(-30, 120))
    assert len(call(client, "GET", f"{DISPATCH}/technicians/{leave}", owner)["leave"]) == 1
    with connection(read_only=True) as conn:
        owner_id = conn.execute("SELECT id FROM staff_users WHERE username='dispatch-owner'").fetchone()[0]
        assert all(not value.startswith("Bearer ") for value, in conn.execute("SELECT verifier FROM technician_sessions"))

    customer = call(client, "POST", CRM + "/customers", owner,
                    {"name": "Synthetic private customer", "operation_key": "dispatch-fixture"})
    with connection() as conn:
        conn.execute("UPDATE customers SET address='Synthetic private address' WHERE id=?", (customer["id"],))
        conn.execute("INSERT INTO crm_contacts(customer_id,mobile,verified_at) VALUES (?,'9000000010','fixture')", (customer["id"],))

    criteria = {"pincode": "673001", "skill": "appliance"}

    def case(name, *, configure=None):
        record = call(client, "POST", CRM + "/cases", owner, {
            "customer_id": customer["id"], "channel": "staff", "description": "Synthetic issue " + name,
            "customer_confirmed": True, "confirmation_text": "Synthetic confirmation provided",
            "operation_key": name,
        })
        case_id = record["id"]
        if configure:
            setup = call(client, "POST", f"{DISPATCH}/cases/{case_id}/assign", owner, configure)
            assert setup["state"] == "awaiting_case_acceptance"
        call(client, "POST", f"{CRM}/cases/{case_id}/transitions", owner,
             {"expected_version": 1, "status": "accepted", "reason": "Accepted synthetic complaint"})
        return case_id

    def state(case_id):
        return call(client, "GET", f"{DISPATCH}/cases/{case_id}", owner)

    def transition(job_id, version, status, headers, expected=200):
        return call(client, "POST", f"{TECH}/jobs/{job_id}/transitions", headers,
                    {"version": version, "status": status, "reason": "Synthetic " + status}, expected)

    assert client.get("/technician").status_code == 200
    assert "My jobs" in client.get("/technician").text
    assert "frame-ancestors 'none'" in client.get("/technician").headers["content-security-policy"]
    assert client.get("/technician/assets/portal.js").status_code == 200
    assert client.get("/technician/assets/portal.css").status_code == 200
    assert client.get(TECH + "/jobs").status_code == 401
    assert client.get(TECH + "/jobs", headers=owner).status_code == 401
    assert client.get(DISPATCH + "/technicians", headers=first).status_code == 401
    assert call(client, "GET", TECH + "/session", first)["code"] == "tech-a"

    case_id = case("automatic", configure=criteria)
    call(client, "PUT", f"{DISPATCH}/cases/{case_id}/appointment-preference", owner, {"text": "Afternoon preferred"}, 204)
    job = state(case_id)["assignments"][0]
    job_id = job["id"]
    assert job["status"] == "offered" and job["technician_id"] == a
    assert state(case_id)["preference"] == "Afternoon preferred" and state(case_id)["appointments"] == []
    preview = call(client, "GET", f"{TECH}/jobs/{job_id}", first)
    assert "description" not in preview and "visit_contact" not in preview
    call(client, "GET", f"{TECH}/jobs/{job_id}", second, expected=404)
    transition(job_id, 1, "accepted", second, 404)
    call(client, "PUT", f"{DISPATCH}/assignments/{job_id}/appointment", owner, interval(30), 409)
    assert transition(job_id, 1, "declined", first)["status"] == "declined"
    reassigned = state(case_id)["assignments"][-1]
    assert reassigned["technician_id"] == b and reassigned["status"] == "offered"
    job_id = reassigned["id"]
    call(client, "GET", f"{TECH}/jobs/{job_id}", first, expected=404)
    transition(job_id, 1, "accepted", second)
    transition(job_id, 2, "en_route", second, 409)
    booked = call(client, "PUT", f"{DISPATCH}/assignments/{job_id}/appointment", owner, interval(30))
    assert booked["status"] == "confirmed"
    call(client, "POST", f"{DISPATCH}/technicians/{b}/leave", owner, interval(30), 409)
    call(client, "POST", DISPATCH + "/process-timeouts", owner, expected=204)
    call(client, "POST", DISPATCH + "/process-timeouts", owner, expected=204)
    alerts = call(client, "GET", TECH + "/notifications", second)["items"]
    assert [item["kind"] for item in alerts].count("appointment_reminder") == 1
    dispatch_target = f"{DISPATCH}/assignments/{job_id}/appointment"
    call(client, "PUT", dispatch_target, owner, interval(45, version=9), 409)
    booked = call(client, "PUT", dispatch_target, owner, interval(45, version=1))
    assert booked["version"] == 2
    details = call(client, "GET", f"{TECH}/jobs/{job_id}", second)
    assert details["visit_contact"]["verified_mobile"] == "9000000010"
    assert "Synthetic private address" == details["visit_contact"]["address"]
    call(client, "POST", f"{TECH}/jobs/{job_id}/notes", second,
         {"kind": "escalation", "text": "Synthetic escalation for dispatch"})
    assert "technician_escalation" in [item["kind"] for item in call(client, "GET", DISPATCH + "/notifications", owner)["items"]]

    manual = case("manual")
    call(client, "POST", f"{DISPATCH}/cases/{manual}/assign", owner, {**criteria, "technician_id": b}, 422)
    job2 = call(client, "POST", f"{DISPATCH}/cases/{manual}/assign", owner,
                {**criteria, "technician_id": b, "reason": "Owner manual selection"})["assignment"]
    transition(job2["id"], 1, "accepted", second)
    slots = call(client, "GET", f"{DISPATCH}/assignments/{job2['id']}/slots?day={NOW.date()}", owner)["items"]
    assert any(slot["starts_at"] == int((NOW + timedelta(minutes=90)).timestamp()) for slot in slots)
    assert all(slot["starts_at"] != int((NOW + timedelta(minutes=60)).timestamp()) for slot in slots)
    call(client, "PUT", f"{DISPATCH}/assignments/{job2['id']}/appointment", owner, interval(55), 409)
    booking2 = call(client, "PUT", f"{DISPATCH}/assignments/{job2['id']}/appointment", owner, interval(90))
    call(client, "POST", f"{DISPATCH}/assignments/{job2['id']}/appointment/close", owner,
         {"version": booking2["version"], "status": "no_show", "reason": "Too soon"}, 409)
    unavailable = case("unassigned", configure={"pincode": "999999", "skill": "appliance"})
    assert state(unavailable)["assignments"] == []
    assert any(item["case_id"] == unavailable and item["kind"] == "unassigned"
               for item in call(client, "GET", DISPATCH + "/notifications", owner)["items"])
    assert unavailable in [item["id"] for item in call(client, "GET", DISPATCH + "/queue", owner)["items"]]
    call(client, "POST", f"{CRM}/cases/{manual}/transitions", owner,
         {"expected_version": 2, "status": "cancelled", "reason": "Concurrent booking"}, 409)

    concurrent = case("concurrent")
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: dispatch.assign(concurrent, "673001", "appliance", owner_id), range(12)))
    assert len({result["assignment"]["id"] for result in outcomes}) == 1
    assert outcomes[0]["assignment"]["technician_id"] == a
    expires = case("capacity", configure=criteria)
    assert state(expires)["assignments"][0]["technician_id"] == a
    full = case("full", configure=criteria)
    assert state(full)["assignments"] == []

    def race_booking(item):
        try:
            start = int((NOW + timedelta(minutes=150)).timestamp())
            return dispatch.save_appointment(item[0], start, start + 1800, "Concurrent booking", owner_id, item[1])["id"]
        except dispatch.CRMError as exc:
            assert exc.status == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(result is not None for result in pool.map(race_booking, [(job_id, 2), (job2["id"], 1)])) == 1

    transition(job_id, 2, "en_route", second)
    transition(job_id, 3, "in_progress", second)
    transition(job_id, 4, "awaiting_parts", second)
    transition(job_id, 5, "in_progress", second)
    transition(job_id, 6, "completed", second)
    assert state(case_id)["appointments"][-1]["status"] == "completed"
    call(client, "GET", f"{TECH}/jobs/{job_id}", second, expected=404)
    clock.value += 31 * 60
    call(client, "POST", DISPATCH + "/process-timeouts", owner, expected=204)
    assert state(concurrent)["assignments"][0]["status"] == "expired"
    assert state(concurrent)["assignments"][-1]["technician_id"] == b
    call(client, "POST", TECH + "/session/end", second, expected=204)
    assert client.get(TECH + "/jobs", headers=second).status_code == 401


def migration_rehearsal(folder):
    database = folder / "phase-three-source.sqlite3"
    backup = folder / "phase-three-backup.sqlite3"
    os.environ["MRCARE_DB_PATH"] = str(database)
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.executescript(SCHEMA + """
            CREATE TABLE staff_users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                                      role TEXT NOT NULL CHECK(role='owner'));
            CREATE TABLE staff_sessions (token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE, expires_at INTEGER NOT NULL);
            CREATE INDEX idx_staff_sessions_expiry ON staff_sessions(expires_at);
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (1,'fixture'),(2,'fixture'),(3,'fixture');
            INSERT INTO staff_users VALUES (1,'migration-owner','owner');
            INSERT INTO customers (id,name,created_at) VALUES (1,'Synthetic migration contact','fixture');
        """ + CRM_SCHEMA)
        conn.execute(
            """INSERT INTO crm_cases(id,reference,customer_id,channel,description,confirmation_text,priority,status,
                                     created_at,updated_at,operation_key,operation_hash)
               VALUES (1,'191126001',1,'staff','Synthetic complaint','Synthetic confirmation','normal','accepted',
                       'fixture','fixture','migration-case','fixture')""",
        )
        conn.execute("INSERT INTO crm_conversations(id,case_id) VALUES (1,1)")
        conn.execute("""INSERT INTO crm_case_history(case_id,version,status,actor_id,source,reason,occurred_at)
                        VALUES (1,1,'pending',1,'staff','Synthetic history','fixture')""")
    assert not database_ready()
    migrate_with_backup(backup)
    assert database_ready()
    with closing(sqlite3.connect(database)) as upgraded, closing(sqlite3.connect(backup)) as previous:
        for table in ("customers", "products", "service_requests", "staff_users", "crm_cases", "crm_case_history", "crm_conversations"):
            assert upgraded.execute(f"SELECT * FROM {table}").fetchall() == previous.execute(f"SELECT * FROM {table}").fetchall()
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
        assert previous.execute("SELECT max(version) FROM schema_migrations").fetchone() == (3,)
        assert upgraded.execute("SELECT count(*) FROM technician_assignments").fetchone() == (0,)


def main():
    with TemporaryDirectory() as folder:
        migration_rehearsal(Path(folder))
        os.environ["MRCARE_DB_PATH"] = str(Path(folder) / "dispatch.sqlite3")
        clock = SimpleNamespace(value=int(NOW.timestamp()))
        with patch.object(dispatch, "time", SimpleNamespace(time=lambda: clock.value)), TestClient(app) as client:
            run(client, clock)
    print("Dispatch checks passed (automatic matching, role isolation, scheduling, expiry, and concurrent assignment).")


if __name__ == "__main__":
    main()
