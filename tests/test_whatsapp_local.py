"""Meta webhook, policy, and voice-to-WhatsApp continuity using synthetic data."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient


# Use only isolated data and fake providers even when the shell has deployment settings.
os.environ.update(MRCARE_DATABASE_URL="", MRCARE_REDIS_URL="", MRCARE_RECORDINGS_BUCKET="",
                  MRCARE_WORKER_ALLOW_EXTERNAL="0", MRCARE_WHATSAPP_SEND_ENABLED="0",
                  MRCARE_SARVAM_OUTBOUND_ENABLED="0")

os.environ["MRCARE_CRM_OTP_SECRET"] = "synthetic-whatsapp-otp-secret-for-local-testing"

from app import call_center, crm, crm_identity, whatsapp
from app.admin_cli import migrate_with_backup
from app.crm_api import CaseCreate, deliver_otp
from app.crm_schema import CRM_SCHEMA
from app.database import SCHEMA, connection, database_ready
from app.dispatch_schema import DISPATCH_SCHEMA
from app.main import app
from app.meta_whatsapp import MetaWhatsApp, configured_meta
from app.staff_auth import issue_owner_session
from app.voice_schema import VOICE_SCHEMA
from app.whatsapp_events_api import app as ingress

API = "/api/whatsapp"
PHONE = "9000000010"
SENDER = "91" + PHONE
PHONE_ID = "1234567890"
WABA = "9876543210"
SIGNING = "synthetic-meta-app-secret"
VERIFY = "synthetic-webhook-verifier"
TEMPLATE = {"template_name": "service_followup", "template_language": "en_US"}


def call(client, method, path, headers, body=None, expected=200):
    result = client.request(method, path, headers=headers, json=body)
    assert result.status_code == expected, (path, result.status_code, result.text)
    return result.json() if result.content else None


def delivery(client, *, messages=(), statuses=(), waba=WABA, phone=PHONE_ID, invalid=False):
    data = {"object": "whatsapp_business_account", "entry": [{"id": waba, "changes": [{
        "field": "messages", "value": {"metadata": {"phone_number_id": phone},
                                        "messages": list(messages), "statuses": list(statuses)},
    }]}]}
    body = json.dumps(data).encode()
    signature = hmac.new(SIGNING.encode(), body, hashlib.sha256).hexdigest()
    return client.post(API + "/webhook", content=body, headers={
        "X-Hub-Signature-256": "sha256=" + ("0" * 64 if invalid else signature),
    })


def message(key, text, at, sender=SENDER):
    return {"id": "wamid." + key, "from": sender, "timestamp": str(at), "type": "text", "text": {"body": text}}


def receipt(key, status, at, recipient=SENDER):
    return {"id": key, "recipient_id": recipient, "status": status, "timestamp": str(at)}


def fixtures():
    headers = {"Authorization": "Bearer " + issue_owner_session("wa-fixture-owner")}
    with connection(read_only=True) as conn:
        actor = conn.execute("SELECT id FROM staff_users WHERE username='wa-fixture-owner'").fetchone()[0]
    customer = crm.create_customer("Synthetic WhatsApp customer", "wa-customer", actor)
    with connection() as conn:
        conn.execute("INSERT INTO crm_contacts(customer_id,mobile,verified_at) VALUES (?,?,?)",
                     (customer["id"], PHONE, "synthetic"))
    cases = [crm.create_case(CaseCreate(
        customer_id=customer["id"], channel="voice", description="Synthetic distinct case " + name,
        confirmation_text="Synthetic customer confirmed", customer_confirmed=True, operation_key=name,
    ).model_dump(), actor) for name in ("one", "two")]
    tokens = [crm_identity.issue_token(case["id"], actor) for case in cases]
    crm.record_interaction(cases[0]["id"], {
        "channel": "voice", "provider": "sarvam", "provider_account": "fixture", "external_id": "voice-1",
        "summary": "Synthetic voice intake", "facts": {"model": "fixture"},
    }, actor)
    return headers, actor, cases, tokens


def check(client, webhook, clock):
    owner, actor, cases, tokens = fixtures()
    sent = []
    codes = []
    mode = SimpleNamespace(outcome="accept")

    def mock(request):
        assert request.url.host == "graph.facebook.com" and request.url.path == f"/v27.0/{PHONE_ID}/messages"
        body = json.loads(request.content)
        assert body["to"] == SENDER and body["messaging_product"] == "whatsapp"
        sent.append(body)
        if mode.outcome == "timeout":
            raise httpx.ReadTimeout("synthetic missing acknowledgement")
        if mode.outcome == "reject":
            return httpx.Response(400, json={"error": {"message": "synthetic rejection"}})
        return httpx.Response(200, json={"messages": [{"id": f"wamid.out-{len(sent)}"}]})

    provider = MetaWhatsApp(PHONE_ID, "v27.0", "synthetic-access-token", transport=httpx.MockTransport(mock))
    verify_params = {"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "plain-123"}
    verified = webhook.get(API + "/webhook", params=verify_params)
    assert verified.status_code == 200 and verified.text == "plain-123"
    assert webhook.get(API + "/webhook", params={**verify_params, "hub.verify_token": "bad"}).status_code == 403
    assert webhook.get("/whatsapp/verify").status_code == 200
    assert webhook.get("/whatsapp/verify.js").status_code == 200
    assert webhook.get(API + "/threads").status_code == 404
    assert client.get(API + "/threads").status_code == 401
    assert client.get(API + "/webhook").status_code == 404

    optin = message("optin", "START", clock.value)
    assert delivery(webhook, messages=[optin], invalid=True).status_code == 401
    assert delivery(webhook, messages=[optin], phone="1111111").status_code == 403
    assert delivery(webhook, messages=[optin], waba="1111111").status_code == 403
    with ThreadPoolExecutor(max_workers=4) as pool:
        observed = list(pool.map(lambda _: delivery(webhook, messages=[optin]), range(6)))
    assert all(item.status_code == 200 for item in observed)
    peer = call(client, "GET", API + "/threads", owner)["items"][0]["id"]
    assert len(call(client, "GET", f"{API}/threads/{peer}", owner)["messages"]) == 1

    def outbox():
        return call(client, "GET", API + "/outbox", owner)["items"]

    def submit(item, expected=200):
        return call(client, "POST", f"{API}/outbox/{item}/submit", owner, expected=expected)

    def inbound(key, text, sender=SENDER):
        clock.value += 1
        assert delivery(webhook, messages=[message(key, text, clock.value, sender)]).status_code == 200

    assert len(outbox()) == 1
    first = outbox()[0]["id"]
    submit(first, 503)
    assert sent == []
    app.dependency_overrides[configured_meta] = lambda: provider
    # A status may reach the webhook before the sender persists the acknowledgement.
    assert delivery(webhook, statuses=[receipt("wamid.out-1", "read", clock.value)]).status_code == 200
    assert submit(first)["delivery"]["status"] == "read"
    assert delivery(webhook, statuses=[receipt("wamid.out-1", "sent", clock.value)] * 2).status_code == 200
    assert outbox()[0]["delivery"]["status"] == "read"
    submit(first, 409)

    first_case, second_case = cases
    case_key, other_key = tokens
    inbound("not-yet", "STATUS")
    submit(outbox()[0]["id"])
    assert first_case["reference"] not in sent[-1]["text"]["body"]
    assert second_case["reference"] not in sent[-1]["text"]["body"]
    inbound("resume-first", "RESUME " + first_case["reference"])
    submit(outbox()[0]["id"])
    assert "https://verify.example.test/whatsapp/verify" in sent[-1]["text"]["body"]
    body = {"reference": first_case["reference"], "token": case_key, "mobile": PHONE}
    call(webhook, "POST", API + "/verification/start", {}, body, 503)
    ingress.dependency_overrides[deliver_otp] = lambda: lambda mobile, code: codes.append((mobile, code))
    call(webhook, "POST", API + "/verification/start", {}, {**body, "mobile": "9000000099"}, 404)
    call(webhook, "POST", API + "/verification/start", {}, {**body, "token": other_key}, 404)
    start = call(webhook, "POST", API + "/verification/start", {}, body)
    assert codes[-1][0] == PHONE
    confirm = {"reference": first_case["reference"], "token": case_key,
               "challenge_id": start["challenge_id"], "code": codes[-1][1]}
    call(client, "POST", "/api/crm/resume/confirm", {}, confirm, 404)
    assert call(webhook, "POST", API + "/verification/confirm", {}, confirm) == {"verified": True}
    call(webhook, "POST", API + "/verification/confirm", {}, confirm, 404)
    inbound("status-one", "നില")
    status_send = outbox()[0]["id"]
    call_center.ingest_inbound({
        "app_id": "synthetic-wa-coordination", "interaction_id": "20261119/wa-coordination",
        "state": "started", "started_at": int(time.time()),
    }, "synthetic-start")
    active_call = call_center.list_calls(None, 1)[0]
    call_center.link_case(active_call["id"], first_case["id"], "Verified synthetic active voice call", actor)
    submit(status_send, 409)
    assert outbox()[0]["state"] == "queued"
    call_center.ingest_inbound({
        "app_id": "synthetic-wa-coordination", "interaction_id": "20261119/wa-coordination",
        "state": "ended", "ended_at": int(time.time()),
    }, "synthetic-end")
    submitted = submit(status_send)
    assert submitted["state"] == "submitted" and submitted["delivery"] is None
    assert first_case["reference"] in sent[-1]["text"]["body"]
    assert second_case["reference"] not in sent[-1]["text"]["body"]
    assert delivery(webhook, statuses=[receipt(submitted["provider_id"], "read", clock.value, "919000000099")]).status_code == 200
    assert outbox()[0]["delivery"] is None
    case = crm.case_detail(first_case["id"])
    assert case["interactions"][0]["channel"] == "voice"
    assert {entry["direction"] for entry in case["whatsapp_messages"]} == {"inbound", "outbound"}
    assert crm.case_detail(second_case["id"])["whatsapp_messages"] == []

    inbound("queued-old-case", "STATUS")
    stale = outbox()[0]["id"]
    inbound("resume-second", "RESUME " + second_case["reference"])
    submit(stale, 409)
    assert next(item for item in outbox() if item["id"] == stale)["state"] == "cancelled"
    second = {"reference": second_case["reference"], "token": other_key, "mobile": PHONE}
    started = call(webhook, "POST", API + "/verification/start", {}, second)
    verified = {"reference": second["reference"], "token": other_key, **started, "code": codes[-1][1]}
    assert call(webhook, "POST", API + "/verification/confirm", {}, verified) == {"verified": True}
    inbound("status-two", "STATUS")
    submit(outbox()[0]["id"])
    assert second_case["reference"] in sent[-1]["text"]["body"]
    assert first_case["reference"] not in sent[-1]["text"]["body"]

    inbound("sensitive-accidental", "HUMAN " + other_key + " 87654321")
    staff = call(client, "GET", API + "/handoffs", owner)["items"]
    assert len(staff) == 1
    body_data = json.dumps(call(client, "GET", f"{API}/threads/{peer}", owner))
    assert other_key not in body_data and "87654321" not in body_data
    before = len(outbox())
    inbound("paused-automation", "STATUS")
    assert len(outbox()) == before
    call(client, "POST", f"{API}/handoffs/{staff[0]['id']}/close", owner, {"reason": "Handled by synthetic staff"}, 204)
    inbound("revoke-check", "STATUS")
    masked_status = outbox()[0]["id"]
    crm_identity.revoke_token(second_case["id"], actor)
    submit(masked_status, 409)

    queued_text = outbox()
    expired_text = next(item["id"] for item in queued_text if item["purpose"] == "system" and item["state"] == "queued")
    clock.value += whatsapp.SERVICE_WINDOW + 1
    submit(expired_text, 409)
    call(client, "POST", f"{API}/threads/{peer}/messages", owner, {"operation_key": "outside", "body": "No case information"}, 409)
    call(client, "POST", f"{API}/threads/{peer}/messages", owner,
         {"operation_key": "unapproved", **TEMPLATE, "template_language": "ml"}, 409)
    payload = {"operation_key": "approved-template", **TEMPLATE}
    approved = call(client, "POST", f"{API}/threads/{peer}/messages", owner, payload)
    assert call(client, "POST", f"{API}/threads/{peer}/messages", owner, payload)["id"] == approved["id"]
    assert submit(approved["id"])["state"] == "submitted"
    assert sent[-1]["template"]["name"] == "service_followup"
    mode.outcome = "timeout"
    lost = call(client, "POST", f"{API}/threads/{peer}/messages", owner, {**payload, "operation_key": "lost"})
    assert submit(lost["id"])["state"] == "uncertain"
    submit(lost["id"], 409)
    mode.outcome = "reject"
    bad = call(client, "POST", f"{API}/threads/{peer}/messages", owner, {**payload, "operation_key": "known-rejection"})
    assert submit(bad["id"])["state"] == "failed"
    to_stop = call(client, "POST", f"{API}/threads/{peer}/messages", owner, {**payload, "operation_key": "opt-out-cancels"})
    clock.value += 60
    mode.outcome = "accept"
    cap = call(client, "POST", f"{API}/threads/{peer}/messages", owner, {**payload, "operation_key": "rate-cap"})
    with patch.dict(os.environ, {"MRCARE_WHATSAPP_RECIPIENT_PER_MINUTE": "1"}):
        assert submit(cap["id"])["state"] == "submitted"
        submit(to_stop["id"], 429)
    assert next(item for item in outbox() if item["id"] == to_stop["id"])["state"] == "queued"
    inbound("stop", "STOP")
    assert next(item for item in outbox() if item["id"] == to_stop["id"])["state"] == "cancelled"
    assert delivery(webhook, messages=[message("late-start", "START", clock.value - 1)]).status_code == 200
    assert call(client, "GET", f"{API}/threads/{peer}", owner)["preference"] == "opted_out"
    call(client, "POST", f"{API}/threads/{peer}/messages", owner, {**payload, "operation_key": "blocked"}, 409)


def migration_rehearsal(directory):
    source, backup = directory / "pre-whatsapp.sqlite3", directory / "pre-whatsapp-backup.sqlite3"
    os.environ["MRCARE_DB_PATH"] = str(source)
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.executescript(SCHEMA + """
            CREATE TABLE staff_users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, role TEXT NOT NULL);
            CREATE TABLE staff_sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL);
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (1,'fixture'),(2,'fixture'),(3,'fixture'),(4,'fixture'),(5,'fixture');
            INSERT INTO customers(id,name,created_at) VALUES (1,'Synthetic migration contact','fixture');
            INSERT INTO service_requests(request_type,customer_id,phone,created_at)
                VALUES ('callback',1,'900000000','fixture');
        """ + CRM_SCHEMA + DISPATCH_SCHEMA + VOICE_SCHEMA)
        conn.execute("""INSERT INTO crm_verification_challenges
                       (id,purpose,customer_id,mobile,verifier,created_at,expires_at)
                       VALUES ('synthetic','enroll',1,'9000000010','fixture',1,2)""")
        conn.execute("""INSERT INTO voice_calls(app_id,direction,interaction_id,state,observed_at,source)
                         VALUES ('synthetic','inbound','20261119/fixture','completed',1,'post_call_webhook')""")
    assert not database_ready()
    migrate_with_backup(backup)
    assert database_ready()
    with closing(sqlite3.connect(source)) as after, closing(sqlite3.connect(backup)) as before:
        for table in ("customers", "service_requests", "voice_calls"):
            assert after.execute(f"SELECT * FROM {table}").fetchall() == before.execute(f"SELECT * FROM {table}").fetchall()
        old_challenge = before.execute("SELECT * FROM crm_verification_challenges").fetchone()
        assert after.execute("SELECT * FROM crm_verification_challenges").fetchone() == (*old_challenge, "crm")
        assert after.execute("PRAGMA foreign_key_check").fetchall() == []


def main():
    os.environ.update(MRCARE_WHATSAPP_PHONE_ID=PHONE_ID, MRCARE_WHATSAPP_WABA_ID=WABA,
                      MRCARE_WHATSAPP_APP_SECRET=SIGNING, MRCARE_WHATSAPP_VERIFY_TOKEN=VERIFY,
                      MRCARE_WHATSAPP_SEND_ENABLED="0", MRCARE_WHATSAPP_APPROVED_TEMPLATES="service_followup:en_US",
                      MRCARE_WHATSAPP_RECIPIENT_PER_MINUTE="40",
                      MRCARE_WHATSAPP_VERIFY_URL="https://verify.example.test/whatsapp/verify")
    with TemporaryDirectory() as folder:
        migration_rehearsal(Path(folder))
        os.environ["MRCARE_DB_PATH"] = str(Path(folder) / "whatsapp-phase.sqlite3")
        clock = SimpleNamespace(value=int(time.time()) - 60)
        try:
            with (
                patch.object(whatsapp, "time", SimpleNamespace(time=lambda: clock.value)),
                TestClient(app) as client,
                TestClient(ingress) as webhook,
            ):
                check(client, webhook, clock)
        finally:
            app.dependency_overrides.clear()
            ingress.dependency_overrides.clear()
    print("WhatsApp checks passed (signed webhook, OTP, case isolation, consent, templates, receipts, and timeouts).")


if __name__ == "__main__":
    main()
