"""Idempotent seeding of FICTIONAL demonstration data for the MR CARE demo database.

Everything this script inserts is fictional demo data. It is not real MR CARE
customer, warranty, or pricing information.

Usage (from the project folder):

  Dry run (default, writes nothing):
    .\\.venv\\Scripts\\python.exe .\\scripts\\seed_demo_data.py

  Apply (takes a verified backup first, then inserts in one transaction):
    .\\.venv\\Scripts\\python.exe .\\scripts\\seed_demo_data.py --apply

  Use another database file (tests):
    .\\.venv\\Scripts\\python.exe .\\scripts\\seed_demo_data.py --db-path C:\\path\\to\\copy.sqlite3

Safety properties:
  * Only INSERT statements are issued. There is no UPDATE, DELETE, or DROP.
  * Rows that already exist are detected by stable demo identifiers and left alone.
  * All inserts happen in one transaction; any failure rolls everything back.
  * Before the first write a timestamped backup is taken with SQLite's online
    backup API and checked with PRAGMA integrity_check.
  * Seeded approved-price rows are always inactive (active = 0).
  * It is never run automatically by the application.

Stop the write API (port 8003) before running with --apply.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEMO_ADDRESS_MARKER = "DEMO DATA - Fictional locality"
DEMO_POLICY_PREFIX = "FICTIONAL DEMO POLICY"
DEMO_PRICE_MARKER = "DEMO ONLY - NOT A REAL MR CARE APPROVAL"

TABLES = ("customers", "products", "approved_prices", "service_requests")

REQUIRED_COLUMNS = {
    "customers": {"id", "name", "address", "phone", "pincode", "created_at"},
    "products": {
        "id", "customer_id", "product_name", "model_number", "serial_number",
        "invoice_number", "warranty_number", "purchase_date", "warranty_status",
        "warranty_expiry_date", "warranty_policy", "created_at",
    },
    "approved_prices": {
        "id", "product_name", "issue_code", "issue_label", "min_price",
        "max_price", "currency", "approved_by", "approved_at", "active",
    },
    "service_requests": {"id", "status"},
}

# One entry per demo customer + product + inactive demo price.
# Day offsets are relative to the day the script is run. expiry_offset None
# means the expiry date is deliberately left NULL (never invented).
DEMO_ENTRIES = [
    {
        "number": "02", "code": "WM", "model": "LG-WM-7001", "product_name": "LG Washing Machine",
        "scenario": "valid",
        "purchase_offset": -180, "warranty_status": "valid", "expiry_offset": 550,
        "policy": f"{DEMO_POLICY_PREFIX}: sample 24-month cover for demonstration only. Not a real warranty.",
        "issue_code": "drum_not_spinning", "issue_label": "Drum not spinning",
        "min_price": 800.0, "max_price": 1500.0,
    },
    {
        "number": "03", "code": "RF", "model": "SAM-RF-2501", "product_name": "Samsung Refrigerator",
        "scenario": "expired",
        "purchase_offset": -900, "warranty_status": "expired", "expiry_offset": -170,
        "policy": f"{DEMO_POLICY_PREFIX}: sample 24-month cover for demonstration only. Not a real warranty.",
        "issue_code": "not_cooling", "issue_label": "Not cooling",
        "min_price": 1200.0, "max_price": 2500.0,
    },
    {
        "number": "04", "code": "AC", "model": "VOL-AC-1801", "product_name": "Voltas Split AC",
        "scenario": "unknown",
        "purchase_offset": -300, "warranty_status": "unknown", "expiry_offset": None,
        "policy": None,
        "issue_code": "gas_refill", "issue_label": "Gas refill",
        "min_price": 2000.0, "max_price": 3500.0,
    },
    {
        "number": "05", "code": "MW", "model": "IFB-MW-2301", "product_name": "IFB Microwave Oven",
        "scenario": "missing_expiry",
        "purchase_offset": -400, "warranty_status": "unknown", "expiry_offset": None,
        "policy": f"{DEMO_POLICY_PREFIX}: expiry date is not on file for this demonstration record.",
        "issue_code": "not_heating", "issue_label": "Not heating",
        "min_price": 700.0, "max_price": 1400.0,
    },
    {
        "number": "06", "code": "WP", "model": "KENT-WP-8001", "product_name": "Kent Water Purifier",
        "scenario": "not_applicable",
        "purchase_offset": -60, "warranty_status": "not_applicable", "expiry_offset": None,
        "policy": f"{DEMO_POLICY_PREFIX}: this demonstration item is recorded as sold without warranty cover.",
        "issue_code": "filter_replacement", "issue_label": "Filter replacement",
        "min_price": 900.0, "max_price": 1800.0,
    },
    {
        "number": "07", "code": "IC", "model": "VG-IC-2001", "product_name": "V-Guard Induction Cooktop",
        "scenario": "policy_requires_verification",
        "purchase_offset": -120, "warranty_status": "unknown", "expiry_offset": None,
        "policy": f"{DEMO_POLICY_PREFIX}: coverage requires technician verification. No coverage is guaranteed.",
        "issue_code": "power_failure", "issue_label": "Does not power on",
        "min_price": 500.0, "max_price": 1100.0,
    },
]


class SeedError(RuntimeError):
    """Raised when seeding cannot proceed safely. Nothing is committed."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_dataset(today: date | None = None) -> dict:
    """Return the demo customers, products and prices for the given run date."""
    today = today or date.today()
    customers, products, prices = [], [], []
    for e in DEMO_ENTRIES:
        key = e["number"]
        code = e["code"]
        customers.append({
            "key": key,
            "name": f"Demo Customer {key}",
            "address": f"{DEMO_ADDRESS_MARKER} {key}, Kerala",
            "phone": None,
            "pincode": None,
        })
        expiry = None
        if e["expiry_offset"] is not None:
            expiry = (today + timedelta(days=e["expiry_offset"])).isoformat()
        products.append({
            "customer_key": key,
            "scenario": e["scenario"],
            "product_name": e["product_name"],
            "model_number": e["model"],
            "serial_number": f"DEMO-SERIAL-{code}-001",
            "invoice_number": f"DEMO-INVOICE-{code}-001",
            "warranty_number": f"DEMO-WARRANTY-{code}-001",
            "purchase_date": (today + timedelta(days=e["purchase_offset"])).isoformat(),
            "warranty_status": e["warranty_status"],
            "warranty_expiry_date": expiry,
            "warranty_policy": e["policy"],
        })
        prices.append({
            "product_name": e["product_name"],
            "issue_code": e["issue_code"],
            "issue_label": f"DEMO ONLY - {e['issue_label']}",
            "min_price": e["min_price"],
            "max_price": e["max_price"],
            "currency": "INR",
            "approved_by": DEMO_PRICE_MARKER,
            "active": 0,
        })
    return {"customers": customers, "products": products, "prices": prices}


def resolve_db_path(override: str | None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    # Same resolution the application uses (.env -> MRCARE_DB_PATH).
    sys.path.insert(0, str(PROJECT_ROOT))
    from app.config import get_db_path
    return Path(get_db_path()).resolve()


def _connect(db_path: Path, read_only: bool) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=10)
    else:
        # isolation_level=None: transactions are controlled explicitly below.
        conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _check_schema(conn: sqlite3.Connection) -> None:
    for table, needed in REQUIRED_COLUMNS.items():
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:
            raise SeedError(f"Table '{table}' does not exist. Refusing to create or migrate the schema.")
        missing = needed - cols
        if missing:
            raise SeedError(f"Table '{table}' lacks columns {sorted(missing)}. A migration would be needed; stopping.")


def _counts(conn: sqlite3.Connection) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def _snapshot(conn: sqlite3.Connection) -> dict:
    """Hash every existing row so it can be proven unchanged afterwards."""
    snap = {}
    for t in TABLES:
        rows = conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
        snap[t] = {
            r["id"]: hashlib.sha256(
                json.dumps([r[k] for k in r.keys()], ensure_ascii=True, default=str).encode("ascii")
            ).hexdigest()
            for r in rows
        }
    return snap


def _process(conn: sqlite3.Connection, dataset: dict, write: bool) -> dict:
    """Decide insert / already-existing / skipped for every entry; insert if write."""
    report = {
        "inserted": {"customers": [], "products": [], "prices": []},
        "existing": {"customers": [], "products": [], "prices": []},
        "skipped": [],
    }
    customer_ids: dict[str, int | None] = {}
    unusable_customers: set[str] = set()

    for c in dataset["customers"]:
        rows = conn.execute(
            "SELECT id FROM customers WHERE name = ? AND address = ?", (c["name"], c["address"])
        ).fetchall()
        if len(rows) > 1:
            unusable_customers.add(c["key"])
            report["skipped"].append(f"customer {c['name']}: {len(rows)} matching rows already exist (ambiguous)")
        elif len(rows) == 1:
            customer_ids[c["key"]] = rows[0]["id"]
            report["existing"]["customers"].append(c["name"])
        else:
            new_id = None
            if write:
                cur = conn.execute(
                    "INSERT INTO customers(name,address,phone,pincode,created_at) VALUES(?,?,?,?,?)",
                    (c["name"], c["address"], c["phone"], c["pincode"], _now_iso()),
                )
                new_id = cur.lastrowid
            customer_ids[c["key"]] = new_id
            report["inserted"]["customers"].append(c["name"])

    for p in dataset["products"]:
        label = f"product {p['product_name']} ({p['serial_number']})"
        if conn.execute("SELECT id FROM products WHERE serial_number = ?", (p["serial_number"],)).fetchone():
            report["existing"]["products"].append(p["serial_number"])
            continue
        if p["customer_key"] in unusable_customers:
            report["skipped"].append(f"{label}: its demo customer could not be resolved")
            continue
        clash = conn.execute(
            "SELECT id FROM products WHERE model_number = ? OR warranty_number = ? OR invoice_number = ?",
            (p["model_number"], p["warranty_number"], p["invoice_number"]),
        ).fetchone()
        if clash:
            report["skipped"].append(f"{label}: model/warranty/invoice number already used by product id {clash['id']}")
            continue
        if write:
            conn.execute(
                "INSERT INTO products(customer_id,product_name,model_number,serial_number,invoice_number,"
                "warranty_number,purchase_date,warranty_status,warranty_expiry_date,warranty_policy,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    customer_ids[p["customer_key"]], p["product_name"], p["model_number"], p["serial_number"],
                    p["invoice_number"], p["warranty_number"], p["purchase_date"], p["warranty_status"],
                    p["warranty_expiry_date"], p["warranty_policy"], _now_iso(),
                ),
            )
        report["inserted"]["products"].append(p["serial_number"])

    for pr in dataset["prices"]:
        key = f"{pr['product_name']} / {pr['issue_code']}"
        if conn.execute(
            "SELECT id FROM approved_prices WHERE product_name = ? AND issue_code = ?",
            (pr["product_name"], pr["issue_code"]),
        ).fetchone():
            report["existing"]["prices"].append(key)
            continue
        if write:
            conn.execute(
                "INSERT INTO approved_prices(product_name,issue_code,issue_label,min_price,max_price,currency,"
                "approved_by,approved_at,active) VALUES(?,?,?,?,?,?,?,?,0)",
                (
                    pr["product_name"], pr["issue_code"], pr["issue_label"], pr["min_price"], pr["max_price"],
                    pr["currency"], pr["approved_by"], _now_iso(),
                ),
            )
        report["inserted"]["prices"].append(key)

    return report


def _verify(conn: sqlite3.Connection, dataset: dict, report: dict, before_counts: dict, before_snapshot: dict) -> None:
    """Run inside the open transaction. Any failure raises and triggers rollback."""
    fk_problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_problems:
        raise SeedError(f"Foreign-key check failed: {len(fk_problems)} violation(s).")

    customers = {c["key"]: c for c in dataset["customers"]}
    present = set(report["inserted"]["products"]) | set(report["existing"]["products"])
    for p in dataset["products"]:
        if p["serial_number"] not in present:
            continue
        for column in ("serial_number", "warranty_number"):
            n = conn.execute(f"SELECT COUNT(*) FROM products WHERE {column} = ?", (p[column],)).fetchone()[0]
            if n != 1:
                raise SeedError(f"{column} {p[column]} appears {n} times; expected exactly 1.")
        if p["serial_number"] in report["inserted"]["products"]:
            row = conn.execute(
                "SELECT c.name, c.address FROM products p JOIN customers c ON c.id = p.customer_id "
                "WHERE p.serial_number = ?", (p["serial_number"],),
            ).fetchone()
            expected = customers[p["customer_key"]]
            if not row or row["name"] != expected["name"] or row["address"] != expected["address"]:
                raise SeedError(f"Product {p['serial_number']} is not linked to {expected['name']}.")

    for pr in dataset["prices"]:
        key = f"{pr['product_name']} / {pr['issue_code']}"
        if key in report["inserted"]["prices"]:
            row = conn.execute(
                "SELECT active, approved_by FROM approved_prices WHERE product_name = ? AND issue_code = ?",
                (pr["product_name"], pr["issue_code"]),
            ).fetchone()
            if row["active"] != 0 or row["approved_by"] != DEMO_PRICE_MARKER:
                raise SeedError(f"Seeded price {key} is not inactive and demo-marked.")

    after_snapshot = _snapshot(conn)
    for table, rows in before_snapshot.items():
        for row_id, digest in rows.items():
            if after_snapshot[table].get(row_id) != digest:
                raise SeedError(f"Pre-existing row {table}.id={row_id} changed or disappeared.")

    after = _counts(conn)
    expected = {
        "customers": before_counts["customers"] + len(report["inserted"]["customers"]),
        "products": before_counts["products"] + len(report["inserted"]["products"]),
        "approved_prices": before_counts["approved_prices"] + len(report["inserted"]["prices"]),
        "service_requests": before_counts["service_requests"],
    }
    if after != expected:
        raise SeedError(f"Record counts are {after}; expected {expected}.")


def backup_database(db_path: Path) -> Path:
    """Timestamped backup via SQLite's online backup API; verified before returning."""
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{db_path.stem}-{stamp}.sqlite3"
    if dest.exists():
        raise SeedError(f"Backup file already exists and will not be overwritten: {dest}")

    src = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    src = _connect(db_path, read_only=True)
    chk = _connect(dest, read_only=True)
    try:
        result = chk.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SeedError(f"Backup integrity check returned: {result}")
        if _counts(chk) != _counts(src):
            raise SeedError("Backup row counts do not match the source database.")
    finally:
        chk.close()
        src.close()
    return dest


def run(db_path, apply: bool = False, today: date | None = None, dataset: dict | None = None,
        make_backup: bool = True) -> dict:
    """Dry-run (default) or apply the demo seed. Returns a report dict."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise SeedError(f"Database file not found: {db_path}. This script never creates a database.")
    dataset = dataset or build_dataset(today)

    ro = _connect(db_path, read_only=True)
    try:
        _check_schema(ro)
        before_counts = _counts(ro)
        preview = _process(ro, dataset, write=False)
    finally:
        ro.close()

    nothing_to_insert = not any(preview["inserted"].values())
    result = {
        "mode": "apply" if apply else "dry-run",
        "db_path": str(db_path),
        "backup": None,
        "counts_before": before_counts,
        "counts_after": before_counts,
        "integrity_check": None,
        **preview,
    }
    if not apply or nothing_to_insert:
        return result

    if make_backup:
        result["backup"] = str(backup_database(db_path))

    conn = _connect(db_path, read_only=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            before_counts = _counts(conn)
            before_snapshot = _snapshot(conn)
            report = _process(conn, dataset, write=True)
            _verify(conn, dataset, report, before_counts, before_snapshot)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        result.update(report)
        result["counts_before"] = before_counts
        result["counts_after"] = _counts(conn)
        result["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result["integrity_check"] != "ok":
            raise SeedError(f"Post-commit integrity check returned: {result['integrity_check']}")
    finally:
        conn.close()
    return result


def _print_report(result: dict) -> None:
    applied = result["mode"] == "apply"
    verb = "Inserted" if applied else "Would insert"
    print(f"Mode: {result['mode'].upper()}")
    print(f"Database: {result['db_path']}")
    if result["backup"]:
        print(f"Backup: {result['backup']}")
    for group in ("customers", "products", "prices"):
        ins, ex = result["inserted"][group], result["existing"][group]
        print(f"{group}: {verb.lower()} {len(ins)}, already existing {len(ex)}")
        for item in ins:
            print(f"  + {item}")
        for item in ex:
            print(f"  = {item}")
    print(f"Skipped: {len(result['skipped'])}")
    for item in result["skipped"]:
        print(f"  ! {item}")
    print(f"Counts before: {result['counts_before']}")
    if applied:
        print(f"Counts after:  {result['counts_after']}")
        if result["integrity_check"]:
            print(f"Integrity check: {result['integrity_check']}")
        if not any(result["inserted"].values()):
            print("Nothing to insert. The database was not modified and no backup was taken.")
    else:
        print("DRY RUN - nothing was written. Re-run with --apply to insert.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Seed fictional MR CARE demo data (dry-run by default).")
    parser.add_argument("--apply", action="store_true", help="Actually write. Without this flag nothing is modified.")
    parser.add_argument("--db-path", default=None, help="Database file to use instead of the configured MRCARE_DB_PATH.")
    args = parser.parse_args(argv)
    try:
        result = run(resolve_db_path(args.db_path), apply=args.apply)
    except (SeedError, sqlite3.Error) as exc:
        print(f"SEED FAILED - nothing was committed: {exc}")
        return 1
    _print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
