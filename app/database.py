import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from .config import get_db_path

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS customers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, address TEXT, phone TEXT, pincode TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER REFERENCES customers(id), product_name TEXT NOT NULL, model_number TEXT, serial_number TEXT UNIQUE, invoice_number TEXT, warranty_number TEXT, purchase_date TEXT, warranty_status TEXT NOT NULL DEFAULT 'unknown', warranty_expiry_date TEXT, warranty_policy TEXT, master_status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approved_prices (
 id INTEGER PRIMARY KEY AUTOINCREMENT, product_name TEXT NOT NULL, issue_code TEXT NOT NULL, issue_label TEXT NOT NULL, min_price REAL, max_price REAL, currency TEXT NOT NULL DEFAULT 'INR', approved_by TEXT NOT NULL, approved_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, UNIQUE(product_name, issue_code)
);
CREATE TABLE IF NOT EXISTS service_requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT, request_type TEXT NOT NULL CHECK(request_type IN ('complaint','callback','appointment')), customer_id INTEGER REFERENCES customers(id), product_id INTEGER REFERENCES products(id), customer_name TEXT, phone TEXT, address TEXT, pincode TEXT, product_name TEXT, model_number TEXT, serial_number TEXT, invoice_number TEXT, warranty_number TEXT, issue_description TEXT, requested_date TEXT, requested_time TEXT, status TEXT NOT NULL DEFAULT 'pending', confirmation_text TEXT, interaction_id TEXT, idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_model ON products(model_number);
CREATE INDEX IF NOT EXISTS idx_products_serial ON products(serial_number);
CREATE INDEX IF NOT EXISTS idx_requests_status ON service_requests(status);
"""

def now_iso():
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def connection():
    conn = sqlite3.connect(get_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with connection() as conn:
        conn.executescript(SCHEMA)
        # Fictional demonstration data only; never real MR CARE customer records.
        conn.execute("INSERT OR IGNORE INTO customers(id,name,address,phone,pincode,created_at) VALUES(1,?,?,?,?,?)", ("Demo Customer", "Demo locality, Kerala", "9000000001", "673001", now_iso()))
        conn.execute("INSERT OR IGNORE INTO products(id,customer_id,product_name,model_number,serial_number,invoice_number,purchase_date,warranty_status,warranty_expiry_date,warranty_policy,created_at) VALUES(1,1,?,?,?,?,?,?,?,?,?)", ("Demo Air Fryer", "DEMO-AF-01", "DEMO-SERIAL-0001", "DEMO-INVOICE-0001", "2026-01-15", "unknown", None, "Demo record: verify policy before any warranty statement.", now_iso()))
