"""Verify money, authorization, immutable documents, and reconciliation locally."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import csv
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


# Use only isolated data and fake providers even when the shell has deployment settings.
os.environ.update(MRCARE_DATABASE_URL="", MRCARE_REDIS_URL="", MRCARE_RECORDINGS_BUCKET="",
                  MRCARE_WORKER_ALLOW_EXTERNAL="0", MRCARE_WHATSAPP_SEND_ENABLED="0",
                  MRCARE_SARVAM_OUTBOUND_ENABLED="0")

from app import crm, dispatch, finance, voice_followups, whatsapp
from app.admin_cli import migrate_with_backup
from app.crm_api import CaseCreate
from app.crm_schema import CRM_SCHEMA
from app.database import SCHEMA, connection, database_ready, now_iso
from app.dispatch_schema import DISPATCH_SCHEMA
from app.main import app
from app.staff_auth import issue_owner_session
from app.voice_schema import VOICE_SCHEMA
from app.whatsapp_schema import WHATSAPP_SCHEMA

API = "/api/finance"
TOTAL = 76707
NET = 72955
TAX = 3752


def http(client, method, path, auth, body=None, status=200):
    response = client.request(method, path, headers=auth, json=body)
    assert response.status_code == status, (path, response.status_code, response.text)
    return response.json() if response.content else None


def bearer(token):
    return {"Authorization": "Bearer " + token}


def fixture():
    owner = bearer(issue_owner_session("synthetic-finance-owner"))
    with connection() as conn:
        actor_id = conn.execute("SELECT id FROM staff_users WHERE username='synthetic-finance-owner'").fetchone()[0]
    customer = crm.create_customer("Synthetic billed customer", "finance-fixture-customer", actor_id)
    with connection() as conn:
        conn.execute("UPDATE customers SET address='Synthetic original billing address',pincode='673001' WHERE id=?",
                     (customer["id"],))
        product, other = (
            conn.execute(
                "INSERT INTO products(customer_id,product_name,serial_number,created_at) VALUES (?,?,?,?)",
                (customer["id"], "Synthetic asset", serial, now_iso()),
            ).lastrowid for serial in ("synthetic-serial", "another-synthetic-serial")
        )
    techs = [
        dispatch.save_technician(code, {"name": "Synthetic finance technician", "capacity": 5, "active": True,
            "pincodes": {"673001"}, "skills": {"appliance"},
            "hours": [{"weekday": day, "start_minute": 0, "end_minute": 1440} for day in range(7)]}, actor_id)
        for code in ("fin-a", "fin-b")
    ]
    jobs = []
    for index in range(3):
        case = crm.create_case(CaseCreate(
            customer_id=customer["id"], product_id=product, channel="staff", description="Synthetic estimate fixture",
            confirmation_text="Synthetic confirmation for finance workflow", customer_confirmed=True,
            operation_key=f"financial-case-{index}",
        ).model_dump(), actor_id)
        crm.transition_case(case["id"], 1, "accepted", "Synthetic test triage", actor_id)
        assignment = dispatch.assign(case["id"], "673001", "appliance", actor_id, selection=techs[0]["id"])["assignment"]
        dispatch.change_job(assignment["id"], 1, "accepted", "Synthetic accepted assignment", techs[0]["id"])
        jobs.append((case["id"], assignment["id"]))
    return owner, actor_id, techs, jobs, customer["id"], product, other


def _book(job, actor):
    start = int(time.time()) + 60
    local = datetime.fromtimestamp(start, crm.KOLKATA)
    if local.hour == 23 and local.minute > 25:
        start = int((local + timedelta(days=1)).replace(hour=9, minute=0).timestamp())
    dispatch.save_appointment(job, start, start + 1800, "Synthetic confirmed booking", actor)


def _complete(job, technician, actor, *, already_booked=False):
    if not already_booked:
        _book(job, actor)
    for version, status in enumerate(("en_route", "in_progress", "completed"), 2):
        dispatch.change_job(job, version, status, "Synthetic visit " + status, technician)


def _prices(client, owner, account, product, other):
    tax_price = {"expected_revision": 0, "kind": "labor", "label": "Synthetic labour", "unit_paise": 10005,
                 "tax_bps": 1800, "tax_label": "Synthetic approved test tax", "classification": "test-class",
                 "active": True, "evidence": "Synthetic reviewed test price"}
    http(client, "PUT", API + "/prices/labor", owner, tax_price, 409)
    http(client, "PUT", API + "/settings/issuer", owner, {"legal_name": "Synthetic issuer",
         "address": "Synthetic issuer address", "tax_registration": "SYNTHETIC-TEST-ONLY",
         "tax_evidence": "Synthetic test tax configuration"})
    http(client, "PUT", API + "/settings/inspection-fee", owner, {"minimum_paise": 49000, "evidence": "Synthetic fee"})
    http(client, "PUT", API + "/prices/labor", account, tax_price, 401)
    http(client, "PUT", API + "/prices/labor", owner, tax_price)
    http(client, "PUT", API + "/prices/inspection", owner, {
        **tax_price, "kind": "inspection", "label": "Synthetic inspection", "unit_paise": 49951, "tax_bps": 0,
    })
    http(client, "PUT", API + "/prices/part", owner, {
        **tax_price, "kind": "part", "label": "Synthetic part", "unit_paise": 999, "tax_bps": 500, "product_id": product,
    })
    http(client, "PUT", API + "/prices/incompatible", owner, {
        **tax_price, "kind": "part", "unit_paise": 999, "tax_bps": 0, "product_id": other,
    })
    assert len(http(client, "GET", API + "/prices", account)["items"]) == 4
    return tax_price


def exercise(client):
    owner, actor, techs, jobs, customer, product, other = fixture()
    finance_token = http(client, "POST", API + "/accounts/session", owner, {"username": "synthetic-accounts"})
    account = bearer(finance_token["token"])
    assigned, stranger = (bearer(dispatch.issue_session(tech["id"], actor)) for tech in techs)
    assert client.get("/api/crm/customers", headers=account).status_code == 401
    assert client.get("/api/dispatch/technicians", headers=account).status_code == 401
    assert client.get(API + "/invoices", headers=assigned).status_code == 401
    assert client.get(API + "/invoices").status_code == 401
    labor_price = _prices(client, owner, account, product, other)
    case1, job1 = jobs[0]
    valid_until = datetime.now(timezone.utc) + timedelta(days=2)
    expiry = valid_until.isoformat()
    lines = [
        {"price_code": "inspection", "price_revision": 1, "quantity": 1},
        {"price_code": "labor", "price_revision": 1, "quantity": 2, "discount_paise": 1},
        {"price_code": "part", "price_revision": 1, "quantity": 3, "discount_paise": 2},
    ]
    base = {"expires_at": expiry, "lines": lines, "notes": "Synthetic estimate", "exclusions": "Synthetic exclusions"}
    tech_url = f"/api/technician/jobs/{job1}"
    http(client, "GET", tech_url + "/estimate-catalog", stranger, status=404)
    catalog = http(client, "GET", tech_url + "/estimate-catalog", assigned)
    assert {row["code"] for row in catalog["items"]} == {"inspection", "labor", "part"}
    assert catalog["minimum_inspection_paise"] == 49000
    http(client, "POST", tech_url + "/estimates", assigned, {**base, "operation_key": "bad-fee", "lines": lines[1:]}, 422)
    bad = [{"price_code": "incompatible", "price_revision": 1, "quantity": 1}, *lines]
    http(client, "POST", tech_url + "/estimates", assigned, {**base, "operation_key": "wrong-asset", "lines": bad}, 409)
    http(client, "POST", tech_url + "/estimates", assigned, {**base, "operation_key": "float",
         "lines": [{**lines[0], "discount_paise": 1.5}]}, 422)
    http(client, "POST", tech_url + "/estimates", stranger, {**base, "operation_key": "broken-auth"}, 404)
    initial = http(client, "POST", tech_url + "/estimates", assigned, {**base, "operation_key": "tech-review"})
    assert initial["status"] == "submitted" and initial["total_paise"] == TOTAL
    assert http(client, "POST", tech_url + "/estimates", assigned, {**base, "operation_key": "tech-review"}) == initial

    with connection(read_only=True) as conn:
        assert finance.customer_estimate(conn, case1, int(time.time())) is None
    labor_price = {**labor_price, "expected_revision": 1}
    http(client, "PUT", API + "/prices/labor", owner, labor_price)
    http(client, "PUT", API + "/prices/labor", owner, labor_price, 409)
    http(client, "POST", f"{API}/estimates/{initial['id']}/approve", account, {"evidence": "Stale approved price"}, 409)
    lines[1] = {**lines[1], "price_revision": 2}

    def estimate(index, key, previous=None):
        return http(client, "POST", API + "/estimates", account,
                    {**base, "case_id": jobs[index][0], "operation_key": key, "previous_id": previous})

    def activate(item, accepted=True):
        estimate_path = f"{API}/estimates/{item['id']}"
        assert http(client, "POST", estimate_path + "/approve", account, {"evidence": "Synthetic internal approval"})["status"] == "approved"
        return http(client, "POST", estimate_path + "/decision", account,
                    {"accepted": accepted, "evidence": "Synthetic verified customer decision"})

    current = estimate(0, "approved-revision", initial["id"])
    assert current["net_paise"] == NET and current["tax_paise"] == TAX
    assert [row["tax_paise"] for row in current["lines"]] == [0, 3602, 150]
    assert http(client, "GET", f"{API}/estimates/{initial['id']}", account)["status"] == "superseded"
    assert activate(current)["status"] == "accepted"
    with connection(read_only=True) as conn:
        assert finance.customer_estimate(conn, case1, int(time.time()))["total_paise"] == TOTAL
        assert "Approved estimate ₹767.07" in whatsapp._case_status_text(
            conn, {"id": case1, "reference": "synthetic", "status": "accepted"}, int(time.time()),
        )
        assert finance.customer_estimate(conn, case1, int(valid_until.timestamp()) + 1) is None
    invoice_body = {"operation_key": "test-invoice", "estimate_id": current["id"]}
    http(client, "POST", API + "/invoices", account, invoice_body, 409)
    _book(job1, actor)
    with connection() as conn:
        contact = conn.execute(
            "INSERT INTO crm_contacts(customer_id,mobile,verified_at) VALUES (?,'9000000020','fixture')", (customer,),
        ).lastrowid
    voice_followups.set_permission(contact, True, "Synthetic permission for testing", actor)
    followup = voice_followups.queue_task(case1, contact, "synthetic-finance-brief", None, actor)
    brief = http(client, "GET", f"/api/call-centre/followups/{followup['id']}/brief", owner)
    assert brief["approved_estimate"]["total_paise"] == TOTAL
    _complete(job1, techs[0]["id"], actor, already_booked=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        issued = list(pool.map(lambda _: finance.issue_invoice(current["id"], "test-invoice", "finance:1"), range(10)))
    assert len({result["number"] for result in issued}) == 1
    invoice = issued[0]
    invoice_id = invoice["id"]
    invoice_path = f"{API}/invoices/{invoice_id}"
    assert invoice["snapshot"]["customer"]["address"] == "Synthetic original billing address"
    assert invoice["snapshot"]["product"]["serial_number"] == "synthetic-serial"
    assert invoice["gross_paise"] == 72958 and invoice["discount_paise"] == 3
    with connection() as conn:
        conn.execute("UPDATE customers SET address='Synthetic updated address' WHERE id=?", (customer,))
    for table in ("fin_invoices", "fin_estimate_lines"):
        try:
            with connection() as conn:
                conn.execute(f"DELETE FROM {table}")
        except sqlite3.IntegrityError as exc:
            assert "append-only" in str(exc)
        else:
            raise AssertionError("Issued financial documents must be immutable")
    assert http(client, "GET", invoice_path, account)["snapshot"] == invoice["snapshot"]

    def pay(amount, ref):
        return {"amount_paise": amount, "method": "upi", "source": "synthetic-bank",
                "external_id": ref, "evidence": "Synthetic verified bank record"}

    partial = pay(50000, "partial-id")
    def record(_):
        return finance.record_cash(invoice_id, "payment", {**partial, "occurred_at": None}, "finance:1")
    with ThreadPoolExecutor(max_workers=6) as pool:
        recorded = list(pool.map(record, range(8)))
    assert len({event["id"] for event in recorded}) == 1
    assert http(client, "GET", invoice_path, account)["status"] == "partially_paid"
    http(client, "POST", invoice_path + "/payments", account, {**partial, "amount_paise": 50001}, 409)
    http(client, "POST", invoice_path + "/payments", account, pay(26708, "overpay"), 409)
    http(client, "POST", invoice_path + "/payments", account, pay(26707, "final"))
    assert http(client, "GET", invoice_path, account)["status"] == "paid"
    receipt = http(client, "GET", f"{API}/transactions/{recorded[0]['id']}", account)
    assert receipt["document_type"] == "receipt" and receipt["amount_paise"] == 50000
    http(client, "POST", invoice_path + "/void", account, {"evidence": "Paid invoice cannot be voided"}, 409)

    adjustment = {"lines": [{"line_no": 3, "quantity": 1}], "operation_key": "part-credit",
                  "evidence": "Synthetic authorized partial adjustment"}
    credit = http(client, "POST", invoice_path + "/credits", account, adjustment)
    assert credit["net_paise"] == 998 and credit["tax_paise"] == 50 and credit["total_paise"] == 1048
    assert http(client, "POST", invoice_path + "/credits", account, adjustment)["id"] == credit["id"]
    http(client, "POST", invoice_path + "/credits", account,
         {**adjustment, "operation_key": "over-credit", "lines": [{"line_no": 3, "quantity": 3}]}, 409)
    assert http(client, "GET", invoice_path, account)["refund_due_paise"] == 1048
    refund_fields = {"payment_id": recorded[0]["id"], "credit_id": credit["id"]}
    http(client, "POST", invoice_path + "/refunds", account, {**pay(1049, "over-refund"), **refund_fields}, 409)
    first_refund = {**pay(100, "refund-100"), **refund_fields}
    refund = http(client, "POST", invoice_path + "/refunds", account, first_refund)
    assert http(client, "POST", invoice_path + "/refunds", account, first_refund)["number"] == refund["number"]
    http(client, "POST", invoice_path + "/refunds", account, {**pay(948, "refund-948"), **refund_fields})
    assert http(client, "GET", invoice_path, account)["refund_due_paise"] == 0

    second = estimate(1, "second-estimate")
    activate(second)
    _complete(jobs[1][1], techs[0]["id"], actor)
    voided = http(client, "POST", API + "/invoices", account,
                  {"estimate_id": second["id"], "operation_key": "invoice-to-void"})
    http(client, "POST", f"{API}/invoices/{voided['id']}/void", account, {"evidence": "Synthetic duplicate corrected"})
    with connection(read_only=True) as conn:
        assert finance.customer_estimate(conn, jobs[1][0], int(time.time())) is None
    replacement = estimate(1, "replace-estimate", second["id"])
    activate(replacement)
    new = http(client, "POST", API + "/invoices", account, {"estimate_id": replacement["id"], "operation_key": "replacement"})
    assert len({invoice["number"], voided["number"], new["number"]}) == 3
    rejected = estimate(2, "customer-rejected")
    assert activate(rejected, False)["status"] == "rejected"
    expired = estimate(2, "expiry-test", rejected["id"])
    with patch.object(finance, "time", SimpleNamespace(time=lambda: int(valid_until.timestamp()) + 1)):
        assert finance.get_estimate(expired["id"])["status"] == "expired"
        http(client, "POST", f"{API}/estimates/{expired['id']}/approve", account, {"evidence": "Expired"}, 409)

    today = datetime.now(crm.KOLKATA).date()
    query = f"?start={today}&end={today}"
    report = http(client, "GET", API + "/reports/sales" + query, account)
    totals = report["totals"]
    assert totals["invoice_count"] == 3 and totals["billed_paise"] == TOTAL * 3
    assert totals["gross_billed_paise"] == 72958 * 3 and totals["discounts_paise"] == 9
    assert totals["voided_paise"] == TOTAL and totals["credit_notes_paise"] == 1048
    assert totals["net_sales_paise"] == NET * 2 - 998 and totals["net_tax_paise"] == TAX * 2 - 50
    assert totals["payments_paise"] == TOTAL and totals["refunds_paise"] == 1048
    assert totals["net_collected_paise"] + totals["outstanding_paise"] == totals["net_sales_paise"] + totals["net_tax_paise"]
    assert totals["outstanding_paise"] == TOTAL and totals["refund_due_paise"] == 0
    assert totals["parts_revenue_paise"] == 2995 * 2 - 998
    assert totals["service_revenue_paise"] == 20009 * 2
    assert totals["inspection_revenue_paise"] == 49951 * 2
    assert report["operations"]["complaints_invoiced"] == 2
    for group in ("weekly", "monthly"):
        grouped = http(client, "GET", API + "/reports/sales" + query + "&group=" + group, account)
        assert grouped["totals"] == totals and len(grouped["trends"]) == 1
    export = client.get(API + "/reports/sales.csv" + query, headers=account)
    assert export.status_code == 200 and int(next(csv.DictReader(io.StringIO(export.text)))["outstanding_paise"]) == TOTAL
    tomorrow = today + timedelta(days=1)
    later = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 12, tzinfo=crm.KOLKATA)
    with patch.object(finance, "time", SimpleNamespace(time=lambda: int(later.timestamp()))):
        final_credit = http(client, "POST", invoice_path + "/credits", account,
            {**adjustment, "operation_key": "credit-line-remainder", "lines": [{"line_no": 3, "quantity": 2}]})
        assert (final_credit["net_paise"], final_credit["tax_paise"]) == (1997, 100)
    assert http(client, "GET", API + "/reports/sales" + query, account)["totals"] == totals
    later_totals = http(client, "GET", API + f"/reports/sales?start={tomorrow}&end={tomorrow}", account)["totals"]
    assert later_totals["net_sales_paise"] == -1997 and later_totals["refund_due_paise"] == 2097
    with connection() as conn:
        fiscal_before = datetime(2026, 3, 31, 23, 59, tzinfo=crm.KOLKATA)
        fiscal_after = fiscal_before + timedelta(minutes=1)
        assert finance._number(conn, "TEST", int(fiscal_before.timestamp())) == "TEST-2025-000001"
        assert finance._number(conn, "TEST", int(fiscal_after.timestamp())) == "TEST-2026-000001"
    http(client, "POST", API + "/accounts/status", owner, {"username": "synthetic-accounts", "active": False}, 204)
    assert client.get(API + "/invoices", headers=account).status_code == 401
    http(client, "POST", API + "/accounts/session", owner, {"username": "synthetic-accounts"}, 409)


def migration_rehearsal(folder):
    path, backup = folder / "finance-migration.sqlite3", folder / "finance-backup.sqlite3"
    os.environ["MRCARE_DB_PATH"] = str(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(SCHEMA + """
            CREATE TABLE staff_users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, role TEXT NOT NULL);
            CREATE TABLE staff_sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL);
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (1,'fixture'),(2,'fixture'),(3,'fixture'),(4,'fixture'),(5,'fixture'),(6,'fixture');
            INSERT INTO customers(id,name,created_at) VALUES (1,'Synthetic historical caller','fixture');
            INSERT INTO service_requests(request_type,customer_id,phone,created_at)
              VALUES ('callback',1,'900000000','fixture');
            INSERT INTO approved_prices(product_name,issue_code,issue_label,min_price,max_price,approved_by,approved_at,active)
              VALUES ('Synthetic demo','demo','Demo',3.45,6.78,'fixture','fixture',0);
        """ + CRM_SCHEMA + """
            ALTER TABLE crm_verification_challenges ADD COLUMN channel TEXT NOT NULL DEFAULT 'crm';
        """ + DISPATCH_SCHEMA + VOICE_SCHEMA + WHATSAPP_SCHEMA)
        conn.execute("INSERT INTO wa_peers(phone_id,sender) VALUES ('123456','919000000010')")
    assert not database_ready()
    migrate_with_backup(backup)
    assert database_ready()
    with closing(sqlite3.connect(path)) as current, closing(sqlite3.connect(backup)) as previous:
        for table in ("customers", "service_requests", "approved_prices", "wa_peers"):
            assert current.execute(f"SELECT * FROM {table}").fetchall() == previous.execute(f"SELECT * FROM {table}").fetchall()
        assert current.execute("SELECT count(*) FROM fin_price_versions").fetchone() == (0,)
        assert current.execute("PRAGMA foreign_key_check").fetchall() == []


def main():
    with TemporaryDirectory() as folder:
        migration_rehearsal(Path(folder))
        os.environ["MRCARE_DB_PATH"] = str(Path(folder) / "finance.sqlite3")
        with TestClient(app) as client:
            exercise(client)
    print("Finance checks passed (exact estimates, roles, immutable invoices, payment retries, credits, refunds, and reconciliation).")


if __name__ == "__main__":
    main()
