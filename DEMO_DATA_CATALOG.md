# MR CARE Demo Data Catalog

> **Everything in this file is fictional demonstration data.** None of it is real MR CARE customer, product, warranty, or pricing information. Warranty policies and prices here are invented for testing the AI receptionist and carry no commitment of any kind.

The data is inserted by [scripts/seed_demo_data.py](scripts/seed_demo_data.py). The script is dry-run by default, never updates or deletes rows, and can be re-run safely.

> **About the brand names.** The six seeded products use well-known brand names (LG, Samsung, Voltas, IFB, Kent, V-Guard) only so that test calls sound realistic. The model numbers are invented and do not correspond to real catalogue models. Nothing here is real information from, or endorsed by, those brands.

The six products were first seeded as `Demo ...` and renamed once with [scripts/rename_demo_products.py](scripts/rename_demo_products.py). The original `Demo Air Fryer` record was not renamed.

## How to recognise demo data

| Record | Marker |
|---|---|
| Customers | Name `Demo Customer NN`; address starts with `DEMO DATA - Fictional locality`; phone is NULL |
| Products | Serial, invoice and warranty numbers start with `DEMO-`; owned by a `Demo Customer NN` |
| Warranty policies | Text starts with `FICTIONAL DEMO POLICY` |
| Prices | `approved_by` is `DEMO ONLY - NOT A REAL MR CARE APPROVAL`; label starts with `DEMO ONLY`; `active = 0` |

## Customers and products

Dates are calculated from the day the seed script is applied. The dates below are the values for a seed applied on **2026-11-13**.

| Customer | Product | Model | Serial | Invoice | Warranty number | Purchase date |
|---|---|---|---|---|---|---|
| Demo Customer | Demo Air Fryer (original record) | DEMO-AF-01 | DEMO-SERIAL-0001 | DEMO-INVOICE-0001 | none | 2026-01-15 |
| Demo Customer 02 | LG Washing Machine | LG-WM-7001 | DEMO-SERIAL-WM-001 | DEMO-INVOICE-WM-001 | DEMO-WARRANTY-WM-001 | 2026-05-17 (seed day − 180) |
| Demo Customer 03 | Samsung Refrigerator | SAM-RF-2501 | DEMO-SERIAL-RF-001 | DEMO-INVOICE-RF-001 | DEMO-WARRANTY-RF-001 | 2024-05-27 (seed day − 900) |
| Demo Customer 04 | Voltas Split AC | VOL-AC-1801 | DEMO-SERIAL-AC-001 | DEMO-INVOICE-AC-001 | DEMO-WARRANTY-AC-001 | 2026-01-17 (seed day − 300) |
| Demo Customer 05 | IFB Microwave Oven | IFB-MW-2301 | DEMO-SERIAL-MW-001 | DEMO-INVOICE-MW-001 | DEMO-WARRANTY-MW-001 | 2025-10-09 (seed day − 400) |
| Demo Customer 06 | Kent Water Purifier | KENT-WP-8001 | DEMO-SERIAL-WP-001 | DEMO-INVOICE-WP-001 | DEMO-WARRANTY-WP-001 | 2026-09-14 (seed day − 60) |
| Demo Customer 07 | V-Guard Induction Cooktop | VG-IC-2001 | DEMO-SERIAL-IC-001 | DEMO-INVOICE-IC-001 | DEMO-WARRANTY-IC-001 | 2026-07-16 (seed day − 120) |

## Warranty scenarios and expected lookup behaviour

The "lookup result" columns are what the existing `lookup_product()` calculation returns. The read API reports them as `warranty_status` and `warranty_confirmed`.

| Product | Scenario | Stored status | Expiry date | Policy text | Lookup status | Confirmed | What the receptionist should say |
|---|---|---|---|---|---|---|---|
| LG Washing Machine | Valid | valid | 2028-05-16 (seed day + 550) | Fictional 24-month sample cover | `valid` | true | Record shows warranty valid; final coverage still subject to policy check |
| Samsung Refrigerator | Expired | expired | 2026-05-27 (seed day − 170) | Fictional 24-month sample cover | `expired` | true | Record shows warranty expired |
| Voltas Split AC | Unknown | unknown | NULL | none | `unknown` | false | Coverage cannot be confirmed; offer a technician callback |
| IFB Microwave Oven | Missing expiry date | unknown | NULL | Expiry date not on file | `unknown` | false | Coverage cannot be confirmed; no expiry date is given |
| Kent Water Purifier | Not applicable | not_applicable | NULL | Recorded as sold without cover | `not_applicable` | true | Record shows no warranty applies |
| V-Guard Induction Cooktop | Policy requires verification | unknown | NULL | Requires technician verification | `unknown` | false | Coverage cannot be confirmed; offer a technician callback |
| Demo Air Fryer (original) | Unchanged | unknown | NULL | Verify policy first | `unknown` | false | Coverage cannot be confirmed |

Notes:

- The seed never updates rows, so the dates do not move after seeding. The washing machine will read as `expired` after its expiry date passes.
- The read API returns product name, model, serial, warranty status, the confirmed flag and a message. It does not return customer details, purchase dates or expiry dates.
- Looking up an identifier that is not in the database returns `found: false` with the message "Product master will be added."
- Every identifier supplied in one lookup must match the same product. Mixing identifiers from two different products, or giving a correct serial with a wrong product name, returns `found: false`.

## Demonstration prices (all inactive)

| Product | Issue code | Label | Sample range (INR) | Active |
|---|---|---|---|---|
| LG Washing Machine | `drum_not_spinning` | DEMO ONLY - Drum not spinning | 800 – 1,500 | 0 |
| Samsung Refrigerator | `not_cooling` | DEMO ONLY - Not cooling | 1,200 – 2,500 | 0 |
| Voltas Split AC | `gas_refill` | DEMO ONLY - Gas refill | 2,000 – 3,500 | 0 |
| IFB Microwave Oven | `not_heating` | DEMO ONLY - Not heating | 700 – 1,400 | 0 |
| Kent Water Purifier | `filter_replacement` | DEMO ONLY - Filter replacement | 900 – 1,800 | 0 |
| V-Guard Induction Cooktop | `power_failure` | DEMO ONLY - Does not power on | 500 – 1,100 | 0 |

Because every row is inactive, the price lookup returns `price_available: false` for all of them and the receptionist must not quote a price.

### Deliberately activating one demo price

This is a manual administrator action. The seed script never does it, and re-running the seed does not undo it. Only do this for a controlled demonstration, and tell the audience the figure is fictional.

Activate one row:

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/mrcare_demo.sqlite3'); c.execute('UPDATE approved_prices SET active=1 WHERE product_name=? AND issue_code=?', ('LG Washing Machine','drum_not_spinning')); c.commit(); c.close()"
```

Deactivate it again:

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/mrcare_demo.sqlite3'); c.execute('UPDATE approved_prices SET active=0 WHERE product_name=? AND issue_code=?', ('LG Washing Machine','drum_not_spinning')); c.commit(); c.close()"
```

While active, the lookup returns the sample range with the message that it is an estimate range, not a final quote.

## Example things to say to the receptionist

| Say | Expected retrieval |
|---|---|
| "Look up LG Washing Machine." | Found: model LG-WM-7001, serial DEMO-SERIAL-WM-001, warranty `valid`, confirmed |
| "Check warranty for DEMO-WARRANTY-WM-001." | LG Washing Machine, warranty `valid`, confirmed |
| "Check warranty for DEMO-WARRANTY-RF-001." | Samsung Refrigerator, warranty `expired`, confirmed |
| "Look up serial DEMO-SERIAL-AC-001." | Voltas Split AC, warranty `unknown`, not confirmed; callback offered |
| "My model number is IFB-MW-2301. Is it under warranty?" | IFB Microwave Oven, `unknown`, not confirmed; no expiry date stated |
| "Check warranty number DEMO-WARRANTY-WP-001." | Kent Water Purifier, `not_applicable`, confirmed |
| "Look up V-Guard Induction Cooktop." | VG-IC-2001, `unknown`, not confirmed; callback offered |
| "Look up model DEMO-AF-01." | Demo Air Fryer (original record), `unknown`, not confirmed |
| "Check serial DEMO-SERIAL-XX-999." | Not found: "Product master will be added." |
| "What is the service price for a Samsung Refrigerator that is not cooling?" | No approved price (`price_available: false`); callback offered, no figure quoted |
| "How much to fix a LG Washing Machine drum that is not spinning?" | No approved price unless that row was deliberately activated |

## Service requests

No demo callbacks, complaints or appointments are seeded, so the pending queue only contains requests made through the receptionist. Request creation is tested in a temporary database by [tests/test_write_requests_local.py](tests/test_write_requests_local.py).

## Commands

Preview what the seed would insert (writes nothing):

```powershell
.\.venv\Scripts\python.exe .\scripts\seed_demo_data.py
```

Apply the seed (stop the write API on port 8003 first; a verified backup is saved to `data\backups\`):

```powershell
.\.venv\Scripts\python.exe .\scripts\seed_demo_data.py --apply
```

Run the isolated tests:

```powershell
.\.venv\Scripts\python.exe .\tests\test_demo_seed_local.py
```

```powershell
.\.venv\Scripts\python.exe .\tests\test_write_requests_local.py
```

```powershell
.\.venv\Scripts\python.exe .\tests\test_public_write_api_local.py
```
