import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from .config import get_db_path
from .crm_schema import CRM_SCHEMA
from .dispatch_schema import DISPATCH_SCHEMA
from .finance_schema import FINANCE_SCHEMA, IMMUTABLE_FINANCE_TABLES
from .postgres import PostgreSQLConnection, StorageUnavailable, database_url, postgres_connection
from .reliability_schema import RECORDING_STORAGE_COLUMNS, RELIABILITY_SCHEMA
from .voice_schema import VOICE_SCHEMA
from .whatsapp_schema import WHATSAPP_SCHEMA

SCHEMA_VERSION = 8

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

STAFF_SCHEMA = """
CREATE TABLE staff_users (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK(role = 'owner')
);
CREATE TABLE staff_sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE INDEX idx_staff_sessions_expiry ON staff_sessions(expires_at);
"""
OTP_CHANNEL_COLUMN = """ALTER TABLE crm_verification_challenges ADD COLUMN channel TEXT NOT NULL DEFAULT 'crm'
                        CHECK(channel IN ('crm','whatsapp'))"""

def now_iso():
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def connection(*, read_only=False, single_statement=False):
    dsn = database_url()
    if single_statement and not read_only:
        raise ValueError("Single-statement connections are only for independent reads.")
    if dsn:
        with postgres_connection(dsn, read_only=read_only, single_statement=single_statement) as conn:
            yield conn
        return
    path = get_db_path()
    if read_only:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
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

def begin_write(conn, scope=None):
    if isinstance(conn, PostgreSQLConnection):
        conn.begin_write(scope)
    else:
        conn.execute("BEGIN IMMEDIATE")


def lock_event_order(conn):
    if isinstance(conn, PostgreSQLConnection):
        conn.lock_event_order()


def migrate_db():
    if database_url():
        raise RuntimeError("Use app.pg_admin for PostgreSQL migrations.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        if versions != set(range(1, len(versions) + 1)) or len(versions) > SCHEMA_VERSION:
            raise RuntimeError("The database has an unsupported migration history.")
        if 1 not in versions:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES (1, ?)", (now_iso(),))
        if 2 not in versions:
            for statement in STAFF_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES (2, ?)", (now_iso(),))
        if 3 not in versions:
            for statement in CRM_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            for operation in ("UPDATE", "DELETE"):
                conn.execute(f"""CREATE TRIGGER crm_case_history_no_{operation.lower()}
                    BEFORE {operation} ON crm_case_history
                    BEGIN SELECT RAISE(ABORT, 'Case history is append-only'); END""")
            conn.execute("INSERT INTO schema_migrations VALUES (3, ?)", (now_iso(),))
        if 4 not in versions:
            for statement in DISPATCH_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            for operation in ("UPDATE", "DELETE"):
                conn.execute(f"""CREATE TRIGGER dispatch_audit_no_{operation.lower()}
                    BEFORE {operation} ON dispatch_audit
                    BEGIN SELECT RAISE(ABORT, 'Dispatch audit is append-only'); END""")
            conn.execute("INSERT INTO schema_migrations VALUES (4, ?)", (now_iso(),))
        if 5 not in versions:
            for statement in VOICE_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES (5, ?)", (now_iso(),))
        if 6 not in versions:
            conn.execute(OTP_CHANNEL_COLUMN)
            for statement in WHATSAPP_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES (6, ?)", (now_iso(),))
        if 7 not in versions:
            for statement in FINANCE_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            for table in IMMUTABLE_FINANCE_TABLES:
                for operation in ("UPDATE", "DELETE"):
                    conn.execute(f"""CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table}
                        BEGIN SELECT RAISE(ABORT, 'Financial records are append-only'); END""")
            conn.execute("""CREATE TRIGGER fin_estimate_lines_sealed BEFORE INSERT ON fin_estimate_lines
                WHEN EXISTS (SELECT 1 FROM fin_estimate_events WHERE estimate_id=NEW.estimate_id)
                BEGIN SELECT RAISE(ABORT, 'Submitted estimate lines are sealed'); END""")
            conn.execute("INSERT INTO schema_migrations VALUES (7, ?)", (now_iso(),))
        if 8 not in versions:
            for statement in RELIABILITY_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            for statement in RECORDING_STORAGE_COLUMNS:
                conn.execute(statement)
            conn.execute(
                """INSERT INTO ops_jobs(kind,resource_id,operation_key,state,due_at,created_at)
                   SELECT 'whatsapp-submit',id,'whatsapp-submit:'||id,'queued',updated_at,updated_at
                   FROM wa_outbox WHERE state='queued'""",
            )
            conn.execute(
                """INSERT INTO ops_jobs(kind,resource_id,operation_key,state,due_at,created_at)
                   SELECT 'voice-reconcile',id,'voice-reconcile:'||id,'queued',updated_at,updated_at
                   FROM voice_followups WHERE state='submitted'""",
            )
            conn.execute("INSERT INTO schema_migrations VALUES (8, ?)", (now_iso(),))


def init_db():
    """Retain the original demo startup behavior for a new database."""
    migrate_db()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if any(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
               for table in ("customers", "products", "approved_prices", "service_requests")):
            return
        conn.execute("INSERT INTO customers(id,name,address,phone,pincode,created_at) VALUES(1,?,?,?,?,?)", ("Demo Customer", "Demo locality, Kerala", "9000000001", "673001", now_iso()))
        conn.execute("INSERT INTO products(id,customer_id,product_name,model_number,serial_number,invoice_number,purchase_date,warranty_status,warranty_expiry_date,warranty_policy,created_at) VALUES(1,1,?,?,?,?,?,?,?,?,?)", ("Demo Air Fryer", "DEMO-AF-01", "DEMO-SERIAL-0001", "DEMO-INVOICE-0001", "2026-01-15", "unknown", None, "Demo record: verify policy before any warranty statement.", now_iso()))


def database_ready():
    if database_url():
        from psycopg.errors import UndefinedTable

        try:
            with connection(read_only=True) as conn:
                ready = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")} == set(range(1, SCHEMA_VERSION + 1))
            if ready:
                _ready_until[database_url()] = time.monotonic() + 2
            return ready
        except (StorageUnavailable, UndefinedTable):
            return False
    path = get_db_path()
    if not path.is_file():
        return False
    try:
        with connection(read_only=True) as conn:
            versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            return versions == set(range(1, SCHEMA_VERSION + 1))
    except sqlite3.Error:
        return False


_ready_until = {}


def database_available_for_request():
    dsn = database_url()
    if not dsn:
        return database_ready()
    now = time.monotonic()
    if _ready_until.get(dsn, 0) > now:
        return True
    return database_ready()


def start_demo():
    if database_url():
        if not database_ready():
            raise RuntimeError("PostgreSQL must be migrated before serving requests.")
        return
    if get_db_path().exists():
        if not database_ready():
            raise RuntimeError("Run the offline database migration before starting this application.")
        return
    init_db()
