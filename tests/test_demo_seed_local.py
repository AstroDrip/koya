"""Isolated tests for scripts/seed_demo_data.py and the existing lookup workflows.

Run with the project's Python environment:
  .\\.venv\\Scripts\\python.exe .\\tests\\test_demo_seed_local.py

Uses temporary SQLite files and a test-only secret. It never touches the demo DB
configured in .env and removes its temporary folder when it exits.
All data used here is fictional demonstration data.
"""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

# Set these before importing the app: config reads secrets at import time.
tmp_dir = Path(tempfile.mkdtemp(prefix="mrcare-seed-test-"))
test_db = tmp_dir / "seed-test.sqlite3"
test_secret = "TEST_ONLY_" + uuid4().hex + uuid4().hex
os.environ["MRCARE_DB_PATH"] = str(test_db)
os.environ["MRCARE_TOOL_SECRET"] = test_secret

from fastapi.testclient import TestClient  # noqa: E402
from app.database import init_db  # noqa: E402
from app.workflows import create_request, lookup_price, lookup_product  # noqa: E402
from app.public_api import app as read_app  # noqa: E402
import seed_demo_data as seed  # noqa: E402

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


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def counts(path):
    with opened(path) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in seed.TABLES}


def all_rows(path):
    with opened(path) as conn:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() for t in seed.TABLES}


# Expected results of the EXISTING lookup_product() calculation per scenario.
EXPECTED = {
    "valid": ("valid", True),
    "expired": ("expired", True),
    "unknown": ("unknown", False),
    "missing_expiry": ("unknown", False),
    "not_applicable": ("not_applicable", True),
    "policy_requires_verification": ("unknown", False),
}

try:
    # --- Arrange: a database shaped like the real one (seed row + one operational request).
    init_db()
    create_request({
        "request_type": "callback", "customer_name": "TEST ONLY - Earlier Caller", "phone": "900000000",
        "product_name": "Air Fryer", "issue_description": "TEST ONLY - not heating",
        "requested_date": "tomorrow", "requested_time": "11:00 AM", "customer_confirmed": True,
        "interaction_id": "PRE-EXISTING-TEST", "idempotency_key": "PRE-EXISTING-TEST:callback",
    })
    original_rows = all_rows(test_db)
    original_counts = counts(test_db)
    check("Arrange: baseline is 2 customers, 1 product, 0 prices, 1 request",
          original_counts == {"customers": 2, "products": 1, "approved_prices": 0, "service_requests": 1},
          str(original_counts))

    today = date.today()
    dataset = seed.build_dataset(today)

    # --- Dry run writes nothing.
    before_hash = file_hash(test_db)
    dry = seed.run(test_db)
    check("Dry run is the default mode", dry["mode"] == "dry-run")
    check("Dry run reports 6 customers, 6 products, 6 prices to insert",
          [len(dry["inserted"][k]) for k in ("customers", "products", "prices")] == [6, 6, 6])
    check("Dry run leaves the database file byte-identical", file_hash(test_db) == before_hash)
    check("Dry run takes no backup", dry["backup"] is None and not (tmp_dir / "backups").exists())

    # --- Apply.
    applied = seed.run(test_db, apply=True)
    check("Apply inserts 6 customers, 6 products, 6 prices",
          [len(applied["inserted"][k]) for k in ("customers", "products", "prices")] == [6, 6, 6])
    check("Apply skips nothing", applied["skipped"] == [])
    check("Apply reports integrity ok", applied["integrity_check"] == "ok")
    check("Counts after apply are 8 / 7 / 6 / 1",
          counts(test_db) == {"customers": 8, "products": 7, "approved_prices": 6, "service_requests": 1},
          str(counts(test_db)))

    backup = Path(applied["backup"])
    check("Backup file was created before writing", backup.is_file())
    with opened(backup) as b:
        check("Backup passes integrity_check", b.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
    check("Backup holds the pre-seed state", counts(backup) == original_counts)

    # --- Existing rows preserved exactly.
    now_rows = all_rows(test_db)
    for table in seed.TABLES:
        check(f"Pre-existing {table} rows unchanged",
              now_rows[table][: len(original_rows[table])] == original_rows[table])
    check("Service requests table untouched", now_rows["service_requests"] == original_rows["service_requests"])

    # --- Re-running creates no duplicates and does not modify the file.
    after_apply_hash = file_hash(test_db)
    again = seed.run(test_db, apply=True)
    check("Second apply inserts nothing", not any(again["inserted"].values()))
    check("Second apply reports 6 / 6 / 6 already existing",
          [len(again["existing"][k]) for k in ("customers", "products", "prices")] == [6, 6, 6])
    check("Second apply leaves the file byte-identical", file_hash(test_db) == after_apply_hash)
    check("Second apply takes no new backup", again["backup"] is None
          and len(list((tmp_dir / "backups").glob("*.sqlite3"))) == 1)

    # --- Database integrity.
    with opened(test_db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        check("Foreign-key check passes", conn.execute("PRAGMA foreign_key_check").fetchall() == [])
        check("Integrity check returns ok", conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        serials = [r[0] for r in conn.execute("SELECT serial_number FROM products")]
        check("Serial numbers are unique", len(serials) == len(set(serials)) == 7)
        warranties = [r[0] for r in conn.execute("SELECT warranty_number FROM products WHERE warranty_number IS NOT NULL")]
        check("Six unique warranty numbers", len(warranties) == len(set(warranties)) == 6)
        orphans = conn.execute(
            "SELECT COUNT(*) FROM products p LEFT JOIN customers c ON c.id = p.customer_id WHERE c.id IS NULL"
        ).fetchone()[0]
        check("Every product links to an existing customer", orphans == 0)
        linked_to_caller = conn.execute(
            "SELECT COUNT(*) FROM products WHERE customer_id = 2"
        ).fetchone()[0]
        check("No product is linked to the earlier test caller (customer 2)", linked_to_caller == 0)
        demo_customers = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE address LIKE ? AND phone IS NULL", (seed.DEMO_ADDRESS_MARKER + "%",)
        ).fetchone()[0]
        check("Six demo-marked customers with NULL phone", demo_customers == 6)
        check("All seeded prices are inactive and demo-marked", conn.execute(
            "SELECT COUNT(*) FROM approved_prices WHERE active = 0 AND approved_by = ?", (seed.DEMO_PRICE_MARKER,)
        ).fetchone()[0] == 6)

    # --- Product lookup through the existing workflow, by every identifier.
    customers = {c["key"]: c["name"] for c in dataset["customers"]}
    for p in dataset["products"]:
        for field in ("product_name", "model_number", "serial_number", "warranty_number"):
            result = lookup_product(**{field: p[field]})
            ok = (result.get("found") and result["product"]["serial_number"] == p["serial_number"]
                  and result["product"]["customer_name"] == customers[p["customer_key"]])
            check(f"Lookup {p['product_name']} by {field}", ok, str(result))
    check("Product name lookup is case-insensitive",
          lookup_product(product_name="lg washing machine")["product"]["serial_number"] == "DEMO-SERIAL-WM-001")
    check("Consistent multi-field lookup returns the right product",
          lookup_product(product_name="Samsung Refrigerator", model_number="SAM-RF-2501",
                         warranty_number="DEMO-WARRANTY-RF-001")["product"]["serial_number"] == "DEMO-SERIAL-RF-001")
    # Every supplied identifier must match the SAME product; conflicts are not found.
    not_found = {"found": False, "message": "Product master will be added."}
    conflicts = {
        "name of one product + serial of another":
            {"product_name": "LG Washing Machine", "serial_number": "DEMO-SERIAL-RF-001"},
        "model of one product + warranty number of another":
            {"model_number": "VOL-AC-1801", "warranty_number": "DEMO-WARRANTY-MW-001"},
        "serial of one product + model of another":
            {"serial_number": "DEMO-SERIAL-WP-001", "model_number": "VG-IC-2001"},
        "valid product name + unknown serial":
            {"product_name": "LG Washing Machine", "serial_number": "DEMO-SERIAL-NOPE-999"},
        "expired product's warranty number + valid product's serial":
            {"warranty_number": "DEMO-WARRANTY-RF-001", "serial_number": "DEMO-SERIAL-WM-001"},
        "original air fryer model + another product's serial":
            {"model_number": "DEMO-AF-01", "serial_number": "DEMO-SERIAL-WM-001"},
    }
    for label, fields in conflicts.items():
        check(f"Conflicting identifiers are not found: {label}", lookup_product(**fields) == not_found)
    check("All four identifiers of one product together are found",
          lookup_product(product_name="Voltas Split AC", model_number="VOL-AC-1801", serial_number="DEMO-SERIAL-AC-001",
                         warranty_number="DEMO-WARRANTY-AC-001")["product"]["serial_number"] == "DEMO-SERIAL-AC-001")
    check("No identifiers at all still asks for one",
          lookup_product() == {"found": False, "message": "Provide a product name, model number, serial number, or warranty number to search."})
    for field, value in (("serial_number", "DEMO-SERIAL-NOPE-999"), ("warranty_number", "DEMO-WARRANTY-NOPE-999"),
                         ("model_number", "NOPE-1"), ("product_name", "Demo Toaster")):
        result = lookup_product(**{field: value})
        check(f"Unknown {field} is not found",
              result == {"found": False, "message": "Product master will be added."}, str(result))
    fryer = lookup_product(model_number="DEMO-AF-01")
    check("Existing Demo Air Fryer lookup still succeeds",
          fryer["found"] and fryer["product"]["serial_number"] == "DEMO-SERIAL-0001"
          and fryer["product"]["customer_name"] == "Demo Customer")
    check("Demo Air Fryer warranty still unconfirmed",
          fryer["product"]["warranty_status_calculated"] == "unknown" and fryer["product"]["warranty_confirmed"] is False)

    # --- Warranty scenarios through the existing calculation.
    seen = set()
    for p in dataset["products"]:
        status, confirmed = EXPECTED[p["scenario"]]
        prod = lookup_product(warranty_number=p["warranty_number"])["product"]
        seen.add(p["scenario"])
        check(f"Warranty scenario '{p['scenario']}' -> {status}, confirmed={confirmed}",
              prod["warranty_status_calculated"] == status and prod["warranty_confirmed"] is confirmed, str(prod))
        if not confirmed:
            check(f"  '{p['scenario']}' message says coverage cannot be confirmed",
                  "cannot be confirmed" in prod["message"])
        if p["warranty_expiry_date"] is None:
            check(f"  '{p['scenario']}' expiry date stays NULL (never invented)", prod["warranty_expiry_date"] is None)
        if p["warranty_policy"] is not None:
            check(f"  '{p['scenario']}' policy is marked fictional",
                  prod["warranty_policy"].startswith(seed.DEMO_POLICY_PREFIX))
    check("All six warranty scenarios covered", seen == set(EXPECTED))
    valid = next(p for p in dataset["products"] if p["scenario"] == "valid")
    expired = next(p for p in dataset["products"] if p["scenario"] == "expired")
    check("Valid expiry is in the future, relative to the run date",
          date.fromisoformat(valid["warranty_expiry_date"]) > today)
    check("Expired expiry is in the past, relative to the run date",
          date.fromisoformat(expired["warranty_expiry_date"]) < today)
    later = seed.build_dataset(today + timedelta(days=30))
    check("Dates move with the run date (not hardcoded)",
          later["products"][0]["warranty_expiry_date"] != valid["warranty_expiry_date"])

    # --- The existing read-only API returns the same answers.
    headers = {"X-MRCARE-Tool-Key": test_secret}
    with TestClient(read_app) as client:
        for p in dataset["products"]:
            status, confirmed = EXPECTED[p["scenario"]]
            r = client.post("/api/voice-lookup", headers=headers,
                            json={"action": "check_warranty", "warranty_number": p["warranty_number"]})
            body = r.json()
            check(f"Read API check_warranty {p['warranty_number']}",
                  r.status_code == 200 and body["found"] and body["serial_number"] == p["serial_number"]
                  and body["warranty_status"] == status and body["warranty_confirmed"] is confirmed, r.text)
            check("  Read API response exposes no customer details",
                  not any(k.startswith("customer") for k in body))
        r = client.post("/api/voice-lookup", headers=headers,
                        json={"action": "lookup_product", "serial_number": "DEMO-SERIAL-NOPE-999"})
        check("Read API unknown serial -> found false",
              r.status_code == 200 and r.json() == {"ok": True, "found": False, "message": "Product master will be added."})
        r = client.post("/api/voice-lookup", json={"action": "lookup_product", "model_number": "LG-WM-7001"})
        check("Read API without key -> 401", r.status_code == 401)
        r = client.post("/api/voice-lookup", headers=headers,
                        json={"action": "request_callback", "customer_name": "x", "phone": "9000000001"})
        check("Read API rejects write actions -> 422", r.status_code == 422)
        for pr in dataset["prices"]:
            r = client.post("/api/voice-lookup", headers=headers, json={
                "action": "lookup_approved_price", "product_name": pr["product_name"], "issue_code": pr["issue_code"]})
            check(f"Read API inactive price {pr['issue_code']} -> price_available false",
                  r.status_code == 200 and r.json()["price_available"] is False and "estimate" not in r.json())

    # --- Pricing through the workflow.
    for pr in dataset["prices"]:
        check(f"Inactive demo price not returned: {pr['product_name']} / {pr['issue_code']}",
              lookup_price(pr["product_name"], pr["issue_code"])["price_available"] is False)
    check("Unknown issue code returns no approved price",
          lookup_price("LG Washing Machine", "no_such_issue")["price_available"] is False)

    # Positive path: activate ONE price in this temporary database only.
    sample = dataset["prices"][0]
    with opened(test_db) as conn:
        conn.execute("UPDATE approved_prices SET active = 1 WHERE product_name = ? AND issue_code = ?",
                     (sample["product_name"], sample["issue_code"]))
    active = lookup_price(sample["product_name"], sample["issue_code"])
    check("Activated price (temporary DB only) is returned",
          active["price_available"] is True and active["estimate"]["min_price"] == sample["min_price"]
          and active["estimate"]["max_price"] == sample["max_price"]
          and active["estimate"]["approved_by"] == seed.DEMO_PRICE_MARKER, str(active))
    check("Other prices remain unavailable",
          lookup_price(dataset["prices"][1]["product_name"], dataset["prices"][1]["issue_code"])["price_available"] is False)
    after_activation = seed.run(test_db, apply=True)
    with opened(test_db) as conn:
        still_active = conn.execute("SELECT active FROM approved_prices WHERE product_name = ? AND issue_code = ?",
                                    (sample["product_name"], sample["issue_code"])).fetchone()[0]
    check("Re-seeding does not overwrite a deliberately activated price",
          still_active == 1 and not any(after_activation["inserted"].values()))

    # --- Failure rolls back everything.
    rollback_db = tmp_dir / "rollback-test.sqlite3"
    os.environ["MRCARE_DB_PATH"] = str(rollback_db)
    init_db()
    os.environ["MRCARE_DB_PATH"] = str(test_db)
    broken = seed.build_dataset(today)
    broken["prices"][5]["min_price"] = None
    broken["prices"][5]["approved_by"] = None  # violates NOT NULL after 17 successful inserts
    before = file_hash(rollback_db)
    try:
        seed.run(rollback_db, apply=True, dataset=broken, make_backup=False)
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    check("Failing insert raises", raised)
    check("Failed run rolled back completely (no partial data)",
          counts(rollback_db) == {"customers": 1, "products": 1, "approved_prices": 0, "service_requests": 0}
          and file_hash(rollback_db) == before, str(counts(rollback_db)))

    # A clashing identifier is skipped, not overwritten.
    with opened(rollback_db) as conn:
        conn.execute("INSERT INTO products(customer_id,product_name,model_number,serial_number,created_at) "
                     "VALUES(1,'Someone Else','LG-WM-7001','OTHER-SERIAL','2026-01-01T00:00:00+00:00')")
    clash = seed.run(rollback_db, apply=True, make_backup=False)
    check("Clashing model number is skipped and reported",
          len(clash["skipped"]) == 1 and len(clash["inserted"]["products"]) == 5, str(clash["skipped"]))
    with opened(rollback_db) as conn:
        check("The clashing existing row was not modified", conn.execute(
            "SELECT product_name FROM products WHERE serial_number = 'OTHER-SERIAL'").fetchone()[0] == "Someone Else")

    # --- One-time rename of rows seeded under the old 'Demo ...' names.
    import rename_demo_products as rename  # noqa: E402
    old_db = tmp_dir / "old-names.sqlite3"
    os.environ["MRCARE_DB_PATH"] = str(old_db)
    try:
        init_db()
        create_request({
            "request_type": "callback", "customer_name": "TEST ONLY - Earlier Caller", "phone": "900000000",
            "product_name": "Air Fryer", "issue_description": "TEST ONLY", "requested_date": "tomorrow",
            "customer_confirmed": True, "interaction_id": "OLD-NAMES-TEST", "idempotency_key": "OLD-NAMES-TEST:callback",
        })
        old_dataset = seed.build_dataset(today)
        for entry, product, price in zip(seed.DEMO_ENTRIES, old_dataset["products"], old_dataset["prices"]):
            old_name, old_model = rename.OLD_VALUES[entry["code"]]
            product["product_name"], product["model_number"], price["product_name"] = old_name, old_model, old_name
        seed.run(old_db, apply=True, dataset=old_dataset, make_backup=False)
        check("Arrange: old-name database has the 'Demo' products",
              lookup_product(model_number="DEMO-WM-001")["found"] and not lookup_product(model_number="LG-WM-7001")["found"])
        rows_before = all_rows(old_db)
        counts_before = counts(old_db)

        old_hash = file_hash(old_db)
        dry = rename.run(old_db)
        check("Rename dry run plans 6 products and 6 prices",
              [len(dry["renamed"][k]) for k in ("products", "prices")] == [6, 6] and dry["skipped"] == [])
        check("Rename dry run leaves the file byte-identical", file_hash(old_db) == old_hash)

        done = rename.run(old_db, apply=True)
        check("Rename applies 6 products and 6 prices",
              [len(done["renamed"][k]) for k in ("products", "prices")] == [6, 6] and done["integrity_check"] == "ok")
        check("Rename took a verified backup first", Path(done["backup"]).is_file()
              and counts(done["backup"]) == counts_before)
        check("Rename adds or removes no rows", counts(old_db) == counts_before)
        for p in dataset["products"]:
            for field in ("product_name", "model_number", "serial_number", "warranty_number"):
                found = lookup_product(**{field: p[field]})
                check(f"After rename: {p['product_name']} found by {field}",
                      found.get("found") and found["product"]["serial_number"] == p["serial_number"]
                      and found["product"]["model_number"] == p["model_number"])
        for old_name, old_model in rename.OLD_VALUES.values():
            check(f"After rename: old name '{old_name}' and model are gone",
                  not lookup_product(product_name=old_name)["found"] and not lookup_product(model_number=old_model)["found"])
        rows_after = all_rows(old_db)
        check("Rename left customers and service requests identical",
              rows_after["customers"] == rows_before["customers"]
              and rows_after["service_requests"] == rows_before["service_requests"])
        check("Rename left the original Demo Air Fryer identical", rows_after["products"][0] == rows_before["products"][0])
        with opened(old_db) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(products)")]
        keep = [i for i, c in enumerate(cols) if c not in ("product_name", "model_number")]
        check("Rename changed only product_name and model_number on products",
              [[r[i] for i in keep] for r in rows_after["products"]] == [[r[i] for i in keep] for r in rows_before["products"]])
        with opened(old_db) as conn:
            price_rows = conn.execute("SELECT product_name, active, approved_by FROM approved_prices ORDER BY id").fetchall()
        check("Prices carry the new names and stay inactive and demo-marked",
              [r[0] for r in price_rows] == [p["product_name"] for p in dataset["prices"]]
              and all(r[1] == 0 and r[2] == seed.DEMO_PRICE_MARKER for r in price_rows))

        renamed_hash = file_hash(old_db)
        again = rename.run(old_db, apply=True)
        check("Second rename is a no-op", not any(again["renamed"].values())
              and [len(again["already"][k]) for k in ("products", "prices")] == [6, 6]
              and file_hash(old_db) == renamed_hash)
        reseed = seed.run(old_db, apply=True)
        check("Seeding after the rename inserts nothing (no duplicates)",
              not any(reseed["inserted"].values()) and counts(old_db) == counts_before)
    finally:
        os.environ["MRCARE_DB_PATH"] = str(test_db)

    # --- Refuses to touch a database without the expected schema, or a missing file.
    empty_db = tmp_dir / "empty.sqlite3"
    sqlite3.connect(empty_db).close()
    for label, target in (("empty database", empty_db), ("missing file", tmp_dir / "does-not-exist.sqlite3")):
        try:
            seed.run(target, apply=True)
            refused = False
        except seed.SeedError:
            refused = True
        check(f"Refuses to seed a {label}", refused)
    check("Missing file was not created", not (tmp_dir / "does-not-exist.sqlite3").exists())

    print(f"ALL DEMO SEED TESTS PASSED ({passed} checks)")
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)
