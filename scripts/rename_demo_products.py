"""One-time rename of the six seeded demo products from 'Demo ...' to branded names.

The brand names are used only to make test calls sound realistic. The records
remain FICTIONAL demonstration data: they are not real LG, Samsung, Voltas, IFB,
Kent, V-Guard or MR CARE product, warranty, or pricing information.

Usage (from the project folder):

  Dry run (default, writes nothing):
    .\\.venv\\Scripts\\python.exe .\\scripts\\rename_demo_products.py

  Apply (verified backup first, one transaction):
    .\\.venv\\Scripts\\python.exe .\\scripts\\rename_demo_products.py --apply

What it changes, and nothing else:
  * products.product_name and products.model_number for the six rows whose
    serial number is DEMO-SERIAL-<code>-001 and which still carry the old values.
  * approved_prices.product_name for the six demo-marked price rows.

Rows already renamed are left alone, so it is safe to run again. Serial, invoice
and warranty numbers, dates, statuses, customers, service requests and the
original Demo Air Fryer are never touched.

Stop the write API (port 8003) before running with --apply.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seed_demo_data as seed  # noqa: E402

# Values written by the first version of the seed script.
OLD_VALUES = {
    "WM": ("Demo Washing Machine", "DEMO-WM-001"),
    "RF": ("Demo Refrigerator", "DEMO-RF-001"),
    "AC": ("Demo Split AC", "DEMO-AC-001"),
    "MW": ("Demo Microwave Oven", "DEMO-MW-001"),
    "WP": ("Demo Water Purifier", "DEMO-WP-001"),
    "IC": ("Demo Induction Cooktop", "DEMO-IC-001"),
}
ALLOWED_PRODUCT_CHANGES = {"product_name", "model_number"}
ALLOWED_PRICE_CHANGES = {"product_name"}


def _targets() -> list[dict]:
    targets = []
    for e in seed.DEMO_ENTRIES:
        old_name, old_model = OLD_VALUES[e["code"]]
        targets.append({
            "serial": f"DEMO-SERIAL-{e['code']}-001",
            "old_name": old_name, "old_model": old_model,
            "new_name": e["product_name"], "new_model": e["model"],
            "issue_code": e["issue_code"],
        })
    return targets


def _process(conn: sqlite3.Connection, write: bool) -> dict:
    report = {"renamed": {"products": [], "prices": []}, "already": {"products": [], "prices": []}, "skipped": []}
    for t in _targets():
        label = f"{t['old_name']} -> {t['new_name']} ({t['new_model']})"
        row = conn.execute(
            "SELECT id, product_name, model_number FROM products WHERE serial_number = ?", (t["serial"],)
        ).fetchone()
        if not row:
            report["skipped"].append(f"product {t['serial']}: not in this database")
        elif (row["product_name"], row["model_number"]) == (t["new_name"], t["new_model"]):
            report["already"]["products"].append(t["serial"])
        elif (row["product_name"], row["model_number"]) != (t["old_name"], t["old_model"]):
            report["skipped"].append(f"product {t['serial']}: has unexpected name/model, left untouched")
        elif conn.execute(
            "SELECT 1 FROM products WHERE model_number = ? AND id != ?", (t["new_model"], row["id"])
        ).fetchone():
            report["skipped"].append(f"product {t['serial']}: model {t['new_model']} is already used by another product")
        else:
            if write:
                conn.execute(
                    "UPDATE products SET product_name = ?, model_number = ? "
                    "WHERE id = ? AND serial_number = ? AND product_name = ? AND model_number = ?",
                    (t["new_name"], t["new_model"], row["id"], t["serial"], t["old_name"], t["old_model"]),
                )
            report["renamed"]["products"].append(label)

        price_label = f"{t['old_name']} / {t['issue_code']} -> {t['new_name']} / {t['issue_code']}"
        new_price = conn.execute(
            "SELECT 1 FROM approved_prices WHERE product_name = ? AND issue_code = ?", (t["new_name"], t["issue_code"])
        ).fetchone()
        old_price = conn.execute(
            "SELECT id FROM approved_prices WHERE product_name = ? AND issue_code = ? AND approved_by = ?",
            (t["old_name"], t["issue_code"], seed.DEMO_PRICE_MARKER),
        ).fetchone()
        if new_price and old_price:
            report["skipped"].append(f"price {t['issue_code']}: both old and new names exist, left untouched")
        elif new_price:
            report["already"]["prices"].append(f"{t['new_name']} / {t['issue_code']}")
        elif not old_price:
            report["skipped"].append(f"price {t['old_name']} / {t['issue_code']}: demo-marked row not found")
        else:
            if write:
                conn.execute(
                    "UPDATE approved_prices SET product_name = ? WHERE id = ? AND approved_by = ?",
                    (t["new_name"], old_price["id"], seed.DEMO_PRICE_MARKER),
                )
            report["renamed"]["prices"].append(price_label)
    return report


def _rows(conn: sqlite3.Connection) -> dict:
    return {
        t: {r["id"]: dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")}
        for t in seed.TABLES
    }


def _verify(conn: sqlite3.Connection, before: dict, report: dict) -> None:
    """Inside the open transaction: prove only the intended columns of the intended rows changed."""
    after = _rows(conn)
    serials = {t["serial"] for t in _targets()}
    changed = {"products": 0, "approved_prices": 0}
    for table in seed.TABLES:
        if set(after[table]) != set(before[table]):
            raise seed.SeedError(f"Row set of {table} changed; a rename must not add or remove rows.")
        for row_id, old in before[table].items():
            new = after[table][row_id]
            diff = {k for k in old if old[k] != new[k]}
            if not diff:
                continue
            if table == "products" and old["serial_number"] in serials and diff <= ALLOWED_PRODUCT_CHANGES:
                changed["products"] += 1
            elif (table == "approved_prices" and old["approved_by"] == seed.DEMO_PRICE_MARKER
                  and diff <= ALLOWED_PRICE_CHANGES and new["active"] == old["active"]):
                changed["approved_prices"] += 1
            else:
                raise seed.SeedError(f"Unexpected change in {table}.id={row_id}: columns {sorted(diff)}.")
    expected = {"products": len(report["renamed"]["products"]), "approved_prices": len(report["renamed"]["prices"])}
    if changed != expected:
        raise seed.SeedError(f"Changed rows {changed} do not match the plan {expected}.")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise seed.SeedError("Foreign-key check failed.")


def run(db_path, apply: bool = False, make_backup: bool = True) -> dict:
    db_path = Path(db_path)
    if not db_path.is_file():
        raise seed.SeedError(f"Database file not found: {db_path}.")
    ro = seed._connect(db_path, read_only=True)
    try:
        seed._check_schema(ro)
        preview = _process(ro, write=False)
    finally:
        ro.close()

    result = {"mode": "apply" if apply else "dry-run", "db_path": str(db_path), "backup": None,
              "integrity_check": None, **preview}
    if not apply or not any(preview["renamed"].values()):
        return result

    if make_backup:
        result["backup"] = str(seed.backup_database(db_path))

    conn = seed._connect(db_path, read_only=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            before = _rows(conn)
            report = _process(conn, write=True)
            _verify(conn, before, report)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        result.update(report)
        result["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result["integrity_check"] != "ok":
            raise seed.SeedError(f"Post-commit integrity check returned: {result['integrity_check']}")
    finally:
        conn.close()
    return result


def _print_report(result: dict) -> None:
    applied = result["mode"] == "apply"
    print(f"Mode: {result['mode'].upper()}")
    print(f"Database: {result['db_path']}")
    if result["backup"]:
        print(f"Backup: {result['backup']}")
    for group in ("products", "prices"):
        print(f"{group}: {'renamed' if applied else 'would rename'} {len(result['renamed'][group])}, "
              f"already renamed {len(result['already'][group])}")
        for item in result["renamed"][group]:
            print(f"  ~ {item}")
    print(f"Skipped: {len(result['skipped'])}")
    for item in result["skipped"]:
        print(f"  ! {item}")
    if applied:
        if result["integrity_check"]:
            print(f"Integrity check: {result['integrity_check']}")
        if not any(result["renamed"].values()):
            print("Nothing to rename. The database was not modified and no backup was taken.")
    else:
        print("DRY RUN - nothing was written. Re-run with --apply to rename.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rename seeded demo products to branded names (dry-run by default).")
    parser.add_argument("--apply", action="store_true", help="Actually write. Without this flag nothing is modified.")
    parser.add_argument("--db-path", default=None, help="Database file to use instead of the configured MRCARE_DB_PATH.")
    args = parser.parse_args(argv)
    try:
        result = run(seed.resolve_db_path(args.db_path), apply=args.apply)
    except (seed.SeedError, sqlite3.Error) as exc:
        print(f"RENAME FAILED - nothing was committed: {exc}")
        return 1
    _print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
