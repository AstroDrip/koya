"""Run isolated, repeatable 50-session HTTP load against local PostgreSQL."""

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.parse import urlsplit

import httpx
from psycopg.conninfo import conninfo_to_dict

from app import crm
from app.database import connection, init_db
from app.pg_admin import import_sqlite
from app.postgres import close_pools
from app.staff_auth import issue_owner_session
from app.workflows import create_request

SESSIONS = 50
READ_KEY = "synthetic-capacity-read-key"
WRITE_KEY = "synthetic-capacity-write-key"
SIGNING = "synthetic-capacity-sarvam-signing-secret"
METRICS = "synthetic-capacity-metrics-key-long"


def _temporary_server(stack, module, env, logs):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log = stack.enter_context(TemporaryDirectory(prefix="koya-load-logs-"))
    log_path = Path(log) / "server.log"
    logs.append((module, log_path))
    output = stack.enter_context(log_path.open("wb"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
        env=env, stdout=output, stderr=output,
    )

    def stop():
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    stack.callback(stop)
    origin = f"http://127.0.0.1:{port}"
    with httpx.Client(trust_env=False, timeout=2) as client:
        for _ in range(80):
            try:
                if client.get(origin + "/readyz").status_code == 200:
                    return origin
            except httpx.TransportError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(.25)
    raise RuntimeError(f"{module} did not become ready.")


def _require_isolated(dsn):
    params = conninfo_to_dict(dsn)
    dbname = params.get("dbname", "")
    if params.get("host") not in ("127.0.0.1", "localhost", "::1") or not dbname.startswith("koya_load_"):
        raise ValueError("Use an empty loopback PostgreSQL database named koya_load_<suffix>.")
    if params.get("hostaddr") not in (None, "127.0.0.1", "::1") or params.get("service"):
        raise ValueError("The load harness requires a directly configured loopback address.")


def _bootstrap(dsn, directory):
    os.environ["MRCARE_DATABASE_URL"] = ""
    snapshot = directory / "koya-synthetic-source.sqlite3"
    os.environ["MRCARE_DB_PATH"] = str(snapshot)
    init_db()
    owner = issue_owner_session("synthetic-capacity-owner")
    with connection() as conn:
        actor = conn.execute("SELECT id FROM staff_users WHERE username='synthetic-capacity-owner'").fetchone()[0]
    customer = crm.create_customer("Synthetic Capacity Customer", "capacity-import-customer", actor)
    legacy = create_request({"request_type": "callback", "customer_name": "Synthetic Historical Callback", "phone": "900000000",
                             "idempotency_key": "synthetic-historical-callback"})
    os.environ["MRCARE_DATABASE_URL"] = dsn
    counts = import_sqlite(snapshot)
    with connection(read_only=True) as conn:
        assert conn.execute("SELECT phone FROM service_requests WHERE id=?", (legacy["request"]["id"],)).fetchone()[0] == "900000000"
    return owner, customer["id"], counts


async def _listen(client, url, owner, last=None):
    headers = {"Authorization": "Bearer " + owner}
    if last is not None:
        headers["Last-Event-ID"] = str(last)
    async with client.stream("GET", url + "/api/operations/events", headers=headers) as stream:
        stream.raise_for_status()
        current = {}
        async for line in stream.aiter_lines():
            if not line:
                if "id" in current:
                    yield current
                current = {}
            elif ":" in line and not line.startswith(":"):
                label, _, value = line.partition(":")
                current[label] = value.lstrip()


def _signature(interaction):
    stamp = int(time.time())
    wall = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    body = json.dumps({
        "app_id": "synthetic-capacity-agent", "deployment_id": "synthetic-capacity-deployment", "app_version": 1,
        "interaction_id": datetime.now(timezone.utc).strftime("%Y%m%d") + "/" + interaction,
        "user_phone_number": "+919000000010", "agent_phone_number": "+918000000010",
        "start_datetime": wall, "end_datetime": wall, "duration": 0,
    }).encode()
    signature = hmac.new(SIGNING.encode(), str(stamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return body, {"Sarvam-Timestamp": str(stamp), "Sarvam-Signature": "sha256=" + signature}


def _percentiles(values):
    ordered = sorted(values)
    return {f"p{p}_ms": round(ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)] * 1000, 1)
            for p in (50, 95, 99)}


async def run_load(main, reads, writes, events, owner, customer, rounds, hold_seconds):
    elapsed, errors, references, event_lag = {}, [], set(), []
    request_ids = set()
    owner_headers = {"Authorization": "Bearer " + owner}
    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(12),
                                 limits=httpx.Limits(max_connections=120, max_keepalive_connections=100)) as client:
        async def post(name, url, **kwargs):
            start = time.perf_counter()
            response = await client.post(url, **kwargs)
            elapsed.setdefault(name, []).append(time.perf_counter() - start)
            if response.status_code != 200:
                errors.append((name, response.status_code))
                raise RuntimeError(f"{name}: HTTP {response.status_code} ({response.text[:300]})")
            return response.json()

        async def session(round_index, index):
            origin, alternate = main[index % 2], main[1 - index % 2]
            lookup_origin = reads[index % len(reads)]
            interaction = f"synthetic-session-{round_index}-{index}"
            lookup = await post("voice_lookup", lookup_origin + "/api/voice-lookup", headers={"X-MRCARE-Tool-Key": READ_KEY},
                                json={"action": "check_warranty", "serial_number": "DEMO-SERIAL-0001"})
            assert lookup["found"] is True and lookup["product_name"] == "Demo Air Fryer"
            case = {"customer_id": customer, "channel": "voice", "description": "Synthetic concurrent complaint",
                    "confirmation_text": "Synthetic customer explicitly confirmed", "customer_confirmed": True,
                    "operation_key": interaction}
            first = await post("case_create", origin + "/api/crm/cases", headers=owner_headers, json=case)
            repeat = await post("case_retry", alternate + "/api/crm/cases", headers=owner_headers, json=case)
            assert first["id"] == repeat["id"] and first["reference"] == repeat["reference"]
            references.add(first["reference"])
            voice = {"action": "register_complaint", "customer_name": "Synthetic Capacity Contact",
                     "phone": "9000000010", "product_name": "Demo Air Fryer", "issue_description": "Synthetic service issue",
                     "customer_confirmed": True, "confirmation_text": "Synthetic customer explicitly confirmed",
                     "interaction_id": interaction, "idempotency_key": interaction}
            recorded = await post("voice_write", writes + "/api/voice-request", headers={"X-MRCARE-Write-Key": WRITE_KEY}, json=voice)
            request_ids.add(recorded["request"]["id"])
            end = time.monotonic() + hold_seconds
            while time.monotonic() < end:
                await asyncio.sleep(min(4, max(0, end - time.monotonic())))
                await post("voice_lookup", lookup_origin + "/api/voice-lookup", headers={"X-MRCARE-Tool-Key": READ_KEY},
                           json={"action": "lookup_product", "serial_number": "DEMO-SERIAL-0001"})
            body, headers = _signature(interaction)
            observed = await post("webhook", events + "/api/sarvam/inbound/complete", content=body, headers=headers)
            duplicate = await post("webhook_retry", events + "/api/sarvam/inbound/complete", content=body, headers=headers)
            assert not observed["duplicate"] and duplicate["duplicate"]

        async with asyncio.timeout((60 + hold_seconds) * rounds):
            cursor = None
            for round_index in range(rounds):
                listener = _listen(client, main[round_index % 2], owner, cursor)
                if cursor is None:
                    snapshot = await anext(listener)
                    assert snapshot["event"] == "snapshot"
                    cursor = int(snapshot["id"])
                collected = set()

                async def collect_events():
                    nonlocal cursor
                    feed = listener
                    while len(collected) < SESSIONS * 3:
                        try:
                            async for notice in feed:
                                assert notice["event"] == "change"
                                key = int(notice["id"])
                                assert key > cursor
                                cursor = key
                                payload = json.loads(notice["data"])
                                collected.add((payload["topic"], payload["resource_id"]))
                                event_lag.append(max(0, time.time() - payload["created_at"]))
                                if len(collected) == SESSIONS * 3:
                                    return
                        finally:
                            await feed.aclose()
                        feed = _listen(client, main[1 - round_index % 2], owner, cursor)

                try:
                    async with asyncio.TaskGroup() as group:
                        group.create_task(collect_events())
                        for index in range(SESSIONS):
                            group.create_task(session(round_index, index))
                finally:
                    await listener.aclose()
                assert len(collected) == SESSIONS * 3
        assert len(references) == len(request_ids) == SESSIONS * rounds
        stats = await client.get(main[0] + "/metrics", headers={"Authorization": "Bearer " + METRICS})
        stats.raise_for_status()
    with connection(read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM crm_cases").fetchone()[0] == SESSIONS * rounds
        assert conn.execute("SELECT count(*) FROM voice_calls").fetchone()[0] == SESSIONS * rounds
        assert conn.execute("SELECT count(*) FROM service_requests").fetchone()[0] == SESSIONS * rounds + 1
    return {"simultaneous_sessions": SESSIONS, "rounds": rounds, "hold_seconds_per_session": hold_seconds,
            "unique_complaints": len(references),
            "unique_voice_writes": len(request_ids), "unexpected_errors": errors,
            "event_delivery_lag_upper_bound": _percentiles(event_lag),
            "latency": {name: {"requests": len(values), **_percentiles(values)} for name, values in elapsed.items()},
            "metrics_excerpt": [line for line in stats.text.splitlines()
                                if line.startswith("mrcare_database_pool")][:15]}


def check_domains(origin, whatsapp_events, owner):
    """Exercise production-database SQL outside the load sample."""
    headers = {"Authorization": "Bearer " + owner}
    with connection(read_only=True) as conn:
        case_id = conn.execute("SELECT min(id) FROM crm_cases").fetchone()[0]
    with httpx.Client(trust_env=False, timeout=8) as client:
        def api(method, path, body=None, *, auth=headers, expect=200):
            response = client.request(method, origin + path, headers=auth, json=body)
            if response.status_code != expect:
                raise RuntimeError(f"Domain verification {path}: HTTP {response.status_code}")
            return response.json() if response.content else None

        api("POST", f"/api/crm/cases/{case_id}/transitions",
            {"expected_version": 1, "status": "accepted", "reason": "Synthetic PostgreSQL workflow"})
        tech = api("PUT", "/api/dispatch/technicians/pg-tech", {
            "name": "Synthetic PostgreSQL technician", "active": True, "capacity": 1,
            "pincodes": ["673001"], "skills": ["appliance"],
            "hours": [{"weekday": day, "start_minute": 0, "end_minute": 1440} for day in range(7)],
        })
        assign = api("POST", f"/api/dispatch/cases/{case_id}/assign", {"pincode": "673001", "skill": "appliance"})
        job = assign["assignment"]["id"]
        tech_token = api("POST", f"/api/dispatch/technicians/{tech['id']}/session")["token"]
        tech_auth = {"Authorization": "Bearer " + tech_token}
        assert len(api("GET", "/api/technician/jobs", auth=tech_auth)["items"]) == 1
        api("POST", f"/api/technician/jobs/{job}/transitions",
            {"version": 1, "status": "accepted", "reason": "Synthetic accepted work"}, auth=tech_auth)
        visit = (datetime.now(crm.KOLKATA) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        api("PUT", f"/api/dispatch/assignments/{job}/appointment",
            {"start": visit.isoformat(), "end": (visit + timedelta(hours=1)).isoformat(), "reason": "Synthetic booking"})
        api("PUT", "/api/finance/settings/issuer", {"legal_name": "Synthetic issuer", "address": "Test fixture"})
        api("PUT", "/api/finance/prices/labor", {
            "expected_revision": 0, "kind": "labor", "label": "Synthetic labor", "unit_paise": 1055,
            "tax_bps": 0, "active": True, "evidence": "Synthetic approved test price",
        })
        estimate = api("POST", f"/api/technician/jobs/{job}/estimates", {
            "operation_key": "synthetic-pg-estimate", "expires_at": (visit + timedelta(days=1)).isoformat(),
            "lines": [{"price_code": "labor", "price_revision": 1, "quantity": 1}],
        }, auth=tech_auth)
        api("POST", f"/api/finance/estimates/{estimate['id']}/approve", {"evidence": "Synthetic approval"})
        api("POST", f"/api/finance/estimates/{estimate['id']}/decision", {"evidence": "Synthetic consent", "accepted": True})
        for version, status in enumerate(("en_route", "in_progress", "completed"), 2):
            api("POST", f"/api/technician/jobs/{job}/transitions",
                {"version": version, "status": status, "reason": f"Test {status}"}, auth=tech_auth)
        invoice = api("POST", "/api/finance/invoices", {
            "operation_key": "synthetic-pg-invoice", "estimate_id": estimate["id"],
        })
        api("POST", f"/api/finance/invoices/{invoice['id']}/payments", {
            "source": "synthetic", "external_id": "synthetic-payment", "method": "cash",
            "amount_paise": 55, "evidence": "Synthetic partial ledger payment",
        })
        today = datetime.now(crm.KOLKATA).date()
        totals = api("GET", f"/api/finance/reports/sales?start={today}&end={today}")["totals"]
        assert (totals["net_sales_paise"], totals["payments_paise"], totals["outstanding_paise"]) == (1055, 55, 1000)
        payload = json.dumps({
            "object": "whatsapp_business_account", "entry": [{"id": "123450", "changes": [{"field": "messages",
                "value": {"metadata": {"phone_number_id": "123456"}, "messages": [
                    {"id": "wamid.synthetic", "from": "919000000010", "timestamp": str(int(time.time())),
                     "type": "text", "text": {"body": "START"}},
                ]}}]}],
        }).encode()
        signature = hmac.new(SIGNING.encode(), payload, hashlib.sha256).hexdigest()
        response = client.post(whatsapp_events + "/api/whatsapp/webhook", content=payload,
                               headers={"X-Hub-Signature-256": "sha256=" + signature})
        response.raise_for_status()
        assert api("GET", "/api/whatsapp/threads")["items"][0]["preference"] == "opted_in"
        assert api("GET", "/api/whatsapp/outbox")["items"][0]["state"] == "queued"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--hold-seconds", type=int, default=0)
    parser.add_argument("--enforce-targets", action="store_true", help="Exit nonzero when an acceptance target is missed.")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 19 or not 0 <= args.hold_seconds <= 3600:
        parser.error("Choose 1–19 rounds and a hold of 0–3600 seconds.")
    dsn = os.getenv("MRCARE_TEST_DATABASE_URL", "")
    _require_isolated(dsn)
    redis_url = os.getenv("MRCARE_REDIS_URL", "").strip()
    if redis_url and urlsplit(redis_url).hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("Use a dedicated loopback Redis instance for the isolated workload.")
    with TemporaryDirectory(prefix="koya-capacity-") as folder, ExitStack() as stack:
        logs = []
        try:
            owner, customer, counts = _bootstrap(dsn, Path(folder))
            env = dict(os.environ, MRCARE_STAFF_AUTH_REQUIRED="1", MRCARE_METRICS_KEY=METRICS,
                       MRCARE_TOOL_SECRET=READ_KEY, MRCARE_WRITE_TOOL_SECRET=WRITE_KEY,
                       MRCARE_SARVAM_INBOUND_APP_ID="synthetic-capacity-agent",
                       MRCARE_SARVAM_INBOUND_DEPLOYMENT_ID="synthetic-capacity-deployment",
                       MRCARE_SARVAM_WEBHOOK_SECRET=SIGNING, MRCARE_WORKER_ALLOW_EXTERNAL="0",
                       MRCARE_RECORDINGS_BUCKET="", MRCARE_SARVAM_OUTBOUND_ENABLED="0", MRCARE_REDIS_URL=redis_url,
                       MRCARE_WHATSAPP_APP_SECRET=SIGNING, MRCARE_WHATSAPP_WABA_ID="123450",
                       MRCARE_WHATSAPP_PHONE_ID="123456", MRCARE_WHATSAPP_SEND_ENABLED="0")
            private = [_temporary_server(stack, "app.main:app", env, logs) for _ in range(2)]
            reads = [_temporary_server(stack, "app.public_api:app", env, logs) for _ in range(2)]
            writes = _temporary_server(stack, "app.public_write_api:app", env, logs)
            events = _temporary_server(stack, "app.voice_events_api:app", env, logs)
            began = time.perf_counter()
            result = asyncio.run(run_load(private, reads, writes, events, owner, customer, args.rounds, args.hold_seconds))
            result["seconds"] = round(time.perf_counter() - began, 2)
            result["imported_verified_rows"] = sum(counts.values())
            whatsapp_events = _temporary_server(stack, "app.whatsapp_events_api:app", env, logs)
            check_domains(private[0], whatsapp_events, owner)
            result["postgres_domain_checks"] = ["dispatch", "technician", "finance", "whatsapp_outbox"]
            failures = []
            for operation, percentile in result["latency"].items():
                if percentile["p95_ms"] > 500 or percentile["p99_ms"] > 1000:
                    failures.append(operation)
            if result["event_delivery_lag_upper_bound"]["p99_ms"] > 2000:
                failures.append("event_delivery")
            result["latency_targets_missed"] = failures
            print(json.dumps(result, indent=2))
            if args.enforce_targets and failures:
                raise SystemExit(1)
        except Exception:
            for module, path in logs:
                errors = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                          if line.startswith(("ERROR", "psycopg.", "RuntimeError"))]
                if errors:
                    print(f"{module}: {' | '.join(errors[-5:])}", file=sys.stderr)
            raise
        finally:
            close_pools()


if __name__ == "__main__":
    main()
