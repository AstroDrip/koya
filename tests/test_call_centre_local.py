"""Sarvam call-centre contracts on synthetic data and an HTTP mock transport."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time

import httpx
from fastapi.testclient import TestClient


# Use only isolated data and fake providers even when the shell has deployment settings.
os.environ.update(MRCARE_DATABASE_URL="", MRCARE_REDIS_URL="", MRCARE_RECORDINGS_BUCKET="",
                  MRCARE_WORKER_ALLOW_EXTERNAL="0", MRCARE_WHATSAPP_SEND_ENABLED="0",
                  MRCARE_SARVAM_OUTBOUND_ENABLED="0")

from app import crm, dispatch, voice_followups
from app.admin_cli import migrate_with_backup
from app.crm_api import CaseCreate
from app.crm_schema import CRM_SCHEMA
from app.database import SCHEMA, connection, database_ready
from app.dispatch_schema import DISPATCH_SCHEMA
from app.main import app
from app.sarvam_voice import OutboundAgent, SarvamVoice, configured_provider
from app.staff_auth import issue_owner_session
from app.voice_events_api import app as ingress

ROOT = "/api/call-centre"
INBOUND = "/api/sarvam/inbound"
APP_ID = "synthetic-inbound-agent"
DEPLOYMENT = "synthetic-deployment"
SIGNING = "synthetic-signed-webhook-fixture"
HOOK_KEY = "synthetic-hook-key-for-isolated-local-tests"
MOBILE = "+919000000010"
AGENT_NUMBER = "+918000000011"
INTERACTION = "20261119/fixture-10:00:00"


def request(client, method, url, headers, payload=None, status=200):
    response = client.request(method, url, headers=headers, json=payload)
    assert response.status_code == status, (url, response.status_code, response.text)
    return response.json() if response.content else None


def signed(client, data, *, timestamp=None, invalid=False, indent=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    body = json.dumps(data, indent=indent).encode()
    digest = hmac.new(SIGNING.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return client.post(INBOUND + "/complete", content=body, headers={
        "Sarvam-Timestamp": timestamp, "Sarvam-Signature": "sha256=" + ("0" * 64 if invalid else digest),
    })


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def case_fixture():
    bearer = {"Authorization": "Bearer " + issue_owner_session("synthetic-voice-owner")}
    with connection(read_only=True) as conn:
        actor = conn.execute("SELECT id FROM staff_users WHERE username='synthetic-voice-owner'").fetchone()[0]
    customer = crm.create_customer("Synthetic Call Customer", "customer-voice-fixture", actor)["id"]
    with connection() as conn:
        contact = conn.execute("INSERT INTO crm_contacts(customer_id,mobile,verified_at) VALUES (?,?,?)",
                               (customer, MOBILE[3:], "synthetic-fixture")).lastrowid
    first, second = [
        crm.create_case(CaseCreate(customer_id=customer, channel="staff", description="Synthetic description",
            confirmation_text="Synthetic explicit confirmation", customer_confirmed=True,
            operation_key=f"case-{index}").model_dump(), actor)["id"] for index in (1, 2)
    ]
    return bearer, actor, customer, contact, first, second


def inbound_checks(client, hooks, owner, customer, case_id, other_case, provider):
    headers = {"X-MRCARE-Voice-Hook-Key": HOOK_KEY}
    started = {"interaction_id": INTERACTION, "user_phone_number": MOBILE}
    now = int(time.time())
    event = {"app_id": APP_ID, "deployment_id": DEPLOYMENT, "app_version": 1,
             "interaction_id": INTERACTION, "user_phone_number": MOBILE, "agent_phone_number": AGENT_NUMBER,
             "start_datetime": timestamp(now - 30), "end_datetime": timestamp(now), "duration": 30,
             "interaction_transcript": [{"role": "user", "en_text": "Synthetic private text"}],
             "output_agent_variables": {"mrcare_disposition": "human_requested", "customer_id": 999}}
    assert hooks.get("/readyz").status_code == 200
    assert client.get("/call-centre").status_code == 200
    assert client.get("/call-centre/board.js").status_code == 200
    assert client.get(ROOT + "/calls").status_code == 401
    assert client.get(INBOUND + "/complete").status_code == 404
    assert hooks.get(ROOT + "/calls").status_code == 404
    assert signed(hooks, event, invalid=True).status_code == 401
    assert signed(hooks, event, timestamp=now - 301).status_code == 401
    assert signed(hooks, event, timestamp=now + 301).status_code == 401
    assert signed(hooks, {**event, "deployment_id": "other"}).status_code == 403
    assert signed(hooks, {**event, "start_datetime": timestamp(now + 200)}).status_code == 422
    assert request(hooks, "POST", INBOUND + "/hooks/start", {}, started, 401)
    request(hooks, "POST", INBOUND + "/hooks/start", headers, started)
    row = request(client, "GET", ROOT + "/calls", owner)["items"][0]
    assert row["state"] == "started" and row["observation"] == "start_observed"
    assert row["caller_number"].endswith("0010") and row["caller_number"] != MOBILE
    assert row["customer_id"] is None
    for category in ("customer_requested", "customer_requested"):
        escalation = request(hooks, "POST", INBOUND + "/escalations", headers, {**started, "category": category})
    assert escalation["duplicate"]
    items = request(client, "GET", ROOT + "/escalations", owner)["items"]
    assert len(items) == 1 and items[0]["call_id"] == row["id"]
    request(client, "POST", ROOT + f"/escalations/{items[0]['id']}/resolve", owner,
            {"reason": "Synthetic staff handoff completed"}, 204)
    request(hooks, "POST", INBOUND + "/hooks/end", headers, started)
    assert request(client, "GET", ROOT + f"/calls/{row['id']}", owner)["state"] == "ended"
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: signed(hooks, event), range(8)))
    assert all(response.status_code == 200 for response in responses)
    assert sum(not response.json()["duplicate"] for response in responses) == 1
    assert signed(hooks, event, indent=2).json()["duplicate"] is True
    assert signed(hooks, {**event, "duration": 31}).status_code == 409
    detail = ROOT + f"/calls/{row['id']}"
    stored = request(client, "GET", detail, owner)
    assert stored["state"] == "completed" and stored["source"] == "post_call_webhook"
    assert stored["duration_seconds"] == 30 and stored["transcript_reported"] == 1
    assert stored["customer_id"] is None and stored["case_id"] is None
    assert "Synthetic private text" not in json.dumps(stored)
    request(client, "GET", detail + "/media/transcript", owner, status=403)
    assert request(client, "PUT", detail + "/case", owner, {"case_id": case_id, "reason": "Verified fixture match"})["customer_id"] == customer
    request(client, "PUT", detail + "/case", owner, {"case_id": other_case, "reason": "Wrong fixture"}, 409)
    request(client, "PUT", detail + "/media-permission", owner,
            {"allowed": True, "evidence": "Synthetic notice verified", "expires_at": timestamp(now + 600)}, 204)
    request(client, "GET", detail + "/media/transcript", owner, status=503)
    app.dependency_overrides[configured_provider] = lambda: provider
    assert request(client, "GET", detail + "/media/transcript", owner)["messages"][0]["content"] == "Synthetic authorized transcript"
    assert request(client, "GET", detail + "/media/trace", owner) == {
        "lifecycle_events": [{"event_type": "ServerToolEvent.on_start", "status": "success"}], "turns": [],
    }
    request(client, "GET", detail + "/media/recording", owner, status=403)
    signed_url = request(client, "POST", detail + "/recording-grant", owner)["url"]
    resource, grant = signed_url.split("#grant=")
    alternate_owner = {"Authorization": "Bearer " + issue_owner_session("synthetic-other-voice-owner")}
    request(client, "GET", resource, {**alternate_owner, "X-MRCARE-Media-Grant": grant}, status=403)
    media_headers = {**owner, "X-MRCARE-Media-Grant": grant}
    recording = client.get(resource, headers=media_headers)
    assert recording.status_code == 200 and recording.headers["cache-control"] == "no-store"
    assert request(client, "GET", detail, owner)["recording"]["sha256"] == hashlib.sha256(recording.content).hexdigest()
    request(client, "PUT", detail + "/media-permission", owner, {"allowed": False, "evidence": "Synthetic revocation"}, 204)
    request(client, "GET", resource, media_headers, status=403)

    later = {**event, "interaction_id": "20261119/completion-first"}
    assert signed(hooks, later).status_code == 200
    request(hooks, "POST", INBOUND + "/hooks/start", headers, {"interaction_id": later["interaction_id"]})
    assert request(client, "GET", ROOT + "/calls", owner)["items"][0]["state"] == "completed"
    request(hooks, "POST", INBOUND + "/hooks/start", headers,
            {"interaction_id": "20261119/old-start", "event_at": timestamp(now - 1000)})
    assert request(client, "GET", ROOT + "/calls", owner)["items"][0]["observation"] == "stale"


def mock_provider():
    state = {"dial_count": 0, "outcome": "no_answer", "uncertain": False, "wrong_contact": False, "wrong_record": False}

    def mock(request):
        assert request.url.host == "apps.sarvam.ai" and request.headers["X-API-Key"] == "synthetic-workspace-key"
        path = request.url.path
        if request.method == "POST":
            state["dial_count"] += 1
            assert path == "/api/outbounds/v1/orgs/test-org/workspaces/test-workspace/outbounds"
            assert json.loads(request.content)["user_config"] == {"user_phone_number": MOBILE}
            if state["uncertain"]:
                raise httpx.ReadTimeout("synthetic timeout")
            return httpx.Response(200, json={"attempt_id": "attempt-" + str(state["dial_count"])})
        if path.endswith("/attempts"):
            attempt = json.loads(request.url.params["filter_conditions"])[0]
            assert attempt["field"] == "attempt_id"
            if state["wrong_record"]:
                return httpx.Response(200, json={"items": [{"attempt_id": "other-attempt"}]})
            return httpx.Response(200, json={"items": [{
                "attempt_id": attempt["value"], "channel_direction": "outbound",
                "user_contact": "+919000000099" if state["wrong_contact"] else MOBILE,
                "connectivity_status": state["outcome"], "end_datetime": timestamp(int(time.time())),
                "duration_in_seconds": 5, "interaction_id": "20261119/connected-outbound",
            }]})
        if "/recordings/" in path:
            return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=b"RIFF\0\0\0\0WAVE")
        if "/traces/" in path:
            return httpx.Response(200, json={"interaction_id": INTERACTION, "lifecycle_events": [
                {"event_type": "ServerToolEvent.on_start", "status": "success", "input": {"unreturned": "fixture"}},
            ]})
        assert "/transcripts/" in path and path.endswith(INTERACTION)
        return httpx.Response(200, json={"interaction_id": INTERACTION, "messages": [
            {"role": "assistant", "content": "Synthetic authorized transcript"},
        ]})

    provider = SarvamVoice("test-org", "test-workspace", "synthetic-workspace-key",
                           outbound=OutboundAgent("synthetic-outbound-agent", 2, "synthetic-connection", AGENT_NUMBER),
                           transport=httpx.MockTransport(mock))
    return provider, state


def outbound_checks(client, owner, actor, contact, case_id, provider_state):
    permission = ROOT + f"/contacts/{contact}/outbound-permission"
    followups = ROOT + "/followups"
    body = {"case_id": case_id, "contact_id": contact, "operation_key": "synthetic-happy-call"}
    request(client, "POST", followups, owner, body, 409)
    request(client, "PUT", permission, owner, {"allowed": True, "evidence": "Synthetic permission"})
    request(client, "POST", followups, owner, body, 409)
    tech = dispatch.save_technician("voice-fixture-tech", {
        "name": "Synthetic technician", "active": True, "capacity": 1,
        "pincodes": {"673001"}, "skills": {"appliance"},
        "hours": [{"weekday": day, "start_minute": 0, "end_minute": 1440} for day in range(7)],
    }, actor)
    crm.transition_case(case_id, 1, "accepted", "Synthetic assignment eligibility", actor)
    job = dispatch.assign(case_id, "673001", "appliance", actor)["assignment"]
    dispatch.change_job(job["id"], 1, "accepted", "Synthetic acceptance", tech["id"])
    appointment = (datetime.now(crm.KOLKATA) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    at = int(appointment.timestamp())
    dispatch.save_appointment(job["id"], at, at + 1800, "Synthetic confirmed visit", actor)
    first = request(client, "POST", followups, owner, body)
    assert request(client, "POST", followups, owner, body)["id"] == first["id"]
    first_url = f"{followups}/{first['id']}"
    assert request(client, "GET", first_url + "/brief", owner)["approved_estimate"] is None
    request(client, "POST", first_url + "/informed", owner, {"reason": "Premature"}, 409)
    saved_provider = app.dependency_overrides.pop(configured_provider)
    request(client, "POST", first_url + "/submit", owner, status=503)
    app.dependency_overrides[configured_provider] = saved_provider
    assert request(client, "POST", first_url + "/submit", owner)["state"] == "submitted"
    request(client, "POST", first_url + "/submit", owner, status=409)
    assert provider_state["dial_count"] == 1
    provider_state["wrong_record"] = True
    assert request(client, "POST", first_url + "/reconcile", owner)["state"] == "submitted"
    provider_state["wrong_record"] = False
    provider_state["wrong_contact"] = True
    request(client, "POST", first_url + "/reconcile", owner, status=502)
    assert request(client, "GET", first_url, owner)["state"] == "submitted"
    provider_state["wrong_contact"] = False
    assert request(client, "POST", first_url + "/reconcile", owner)["state"] == "no_answer"
    retry = request(client, "POST", followups, owner, {**body, "operation_key": "retry", "retry_of": first["id"]})
    second_url = f"{followups}/{retry['id']}"
    provider_state["outcome"] = "connected"
    request(client, "POST", second_url + "/submit", owner)
    connected = request(client, "POST", second_url + "/reconcile", owner)
    assert connected["state"] == "connected" and connected["informed_at"] is None
    assert request(client, "POST", second_url + "/informed", owner, {"reason": "Synthetic QA reviewed delivery"})["informed_at"]
    changed = request(client, "POST", followups, owner, {**body, "operation_key": "rebooked"})
    dispatch.save_appointment(job["id"], at + 1800, at + 3600, "Synthetic new time", actor, version=1)
    request(client, "POST", f"{followups}/{changed['id']}/submit", owner, status=409)
    assert request(client, "GET", f"{followups}/{changed['id']}", owner)["state"] == "cancelled"
    uncertain = request(client, "POST", followups, owner, {**body, "operation_key": "ambiguous"})
    uncertain_url = f"{followups}/{uncertain['id']}"
    provider_state["uncertain"] = True
    assert request(client, "POST", uncertain_url + "/submit", owner)["state"] == "uncertain"
    request(client, "POST", uncertain_url + "/submit", owner, status=409)
    pending = provider_state["dial_count"]
    request(client, "POST", followups, owner, {**body, "operation_key": "do-not-redial"}, 409)
    assert provider_state["dial_count"] == pending
    assert request(client, "POST", uncertain_url + "/review-uncertain", owner,
                   {"reason": "Synthetic staff reviewed and closed uncertain submission"})["state"] == "cancelled"
    last = request(client, "POST", followups, owner, {**body, "operation_key": "permission-revoked"})
    request(client, "PUT", permission, owner, {"allowed": False, "evidence": "Synthetic opt-out"})
    assert request(client, "GET", f"{followups}/{last['id']}", owner)["state"] == "cancelled"


def migration_rehearsal(folder):
    path, backup = folder / "legacy-v4.sqlite3", folder / "verified-v4.sqlite3"
    os.environ["MRCARE_DB_PATH"] = str(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(SCHEMA + """
            CREATE TABLE staff_users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, role TEXT NOT NULL);
            CREATE TABLE staff_sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL);
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (1,'fixture'),(2,'fixture'),(3,'fixture'),(4,'fixture');
            INSERT INTO customers(id,name,created_at) VALUES (7,'Legacy fixture','fixture');
            INSERT INTO service_requests(request_type,customer_id,phone,created_at) VALUES ('callback',7,'900000000','fixture');
        """ + CRM_SCHEMA + DISPATCH_SCHEMA)
        conn.execute("INSERT INTO technicians(id,code,name,active,capacity) VALUES (1,'existing-tech','Legacy fixture',1,1)")
    assert not database_ready()
    migrate_with_backup(backup)
    assert database_ready()
    with closing(sqlite3.connect(path)) as after, closing(sqlite3.connect(backup)) as before:
        for table in ("customers", "service_requests", "technicians", "crm_cases", "dispatch_appointments"):
            assert after.execute(f"SELECT * FROM {table}").fetchall() == before.execute(f"SELECT * FROM {table}").fetchall()
        assert before.execute("SELECT max(version) FROM schema_migrations").fetchone() == (4,)
        assert after.execute("PRAGMA foreign_key_check").fetchall() == []


def main():
    os.environ.update(
        MRCARE_SARVAM_INBOUND_APP_ID=APP_ID, MRCARE_SARVAM_INBOUND_DEPLOYMENT_ID=DEPLOYMENT,
        MRCARE_SARVAM_WEBHOOK_SECRET=SIGNING, MRCARE_SARVAM_HOOK_KEY=HOOK_KEY,
        MRCARE_SARVAM_WORKSPACE_KEY="", MRCARE_SARVAM_OUTBOUND_ENABLED="0",
        MRCARE_RECORDING_SIGNING_SECRET="synthetic-recording-signing-secret-for-tests",
    )
    with TemporaryDirectory() as folder:
        migration_rehearsal(Path(folder))
        os.environ["MRCARE_DB_PATH"] = str(Path(folder) / "call-centre.sqlite3")
        provider, provider_state = mock_provider()
        try:
            with TestClient(app) as client, TestClient(ingress) as hooks:
                owner, actor, customer, contact, first, second = case_fixture()
                inbound_checks(client, hooks, owner, customer, first, second, provider)
                outbound_checks(client, owner, actor, contact, first, provider_state)
        finally:
            app.dependency_overrides.clear()
    print("Call centre checks passed (signed events, ordering, role isolation, media, confirmed visits, and outbound outcomes).")


if __name__ == "__main__":
    main()
