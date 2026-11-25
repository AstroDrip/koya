"""Migration rehearsal and in-process API checks using only synthetic data."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient


# Use only isolated data and fake providers even when the shell has deployment settings.
os.environ.update(MRCARE_DATABASE_URL="", MRCARE_REDIS_URL="", MRCARE_RECORDINGS_BUCKET="",
                  MRCARE_WORKER_ALLOW_EXTERNAL="0", MRCARE_WHATSAPP_SEND_ENABLED="0",
                  MRCARE_SARVAM_OUTBOUND_ENABLED="0")

os.environ["MRCARE_TOOL_SECRET"] = "local-test-read-key"
os.environ["MRCARE_WRITE_TOOL_SECRET"] = "local-test-write-key"
os.environ["MRCARE_STAFF_AUTH_REQUIRED"] = "1"

from app.admin_cli import migrate_with_backup
from app.database import SCHEMA, connection, database_ready
from app.main import app
from app.public_api import app as read_app
from app.public_write_api import app as write_app
from app.staff_auth import issue_owner_session, revoke_owner_sessions
from app.workflows import submit_request


def rows(path):
    with closing(sqlite3.connect(path)) as conn:
        return [conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in ("customers", "products", "approved_prices", "service_requests")]


def main():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "legacy.sqlite3"
        backup = Path(directory) / "verified-backup.sqlite3"
        os.environ["MRCARE_DB_PATH"] = str(path)
        with TestClient(read_app) as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/readyz").status_code == 503
            unavailable = client.post(
                "/api/voice-lookup", headers={"X-MRCARE-Tool-Key": "local-test-read-key"},
                json={"action": "lookup_product", "serial_number": "TEST-A"},
            )
            assert unavailable.status_code == 503
        assert not path.exists()
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO customers(id,name,phone,created_at) VALUES (1,'Legacy fixture','900000000','test')")
            conn.execute("""INSERT INTO service_requests(request_type,customer_id,phone,created_at)
                            VALUES ('callback',1,'900000000','test')""")
            conn.execute("""INSERT INTO products(product_name,model_number,serial_number,created_at)
                            VALUES ('Test unit','TEST-MODEL','TEST-A','test'),
                                   ('Test unit','TEST-MODEL','TEST-B','test')""")
            conn.execute("""INSERT INTO approved_prices(product_name,issue_code,issue_label,min_price,max_price,approved_by,approved_at,active)
                            VALUES ('Test unit','TEST','Test price',10,20,'test','test',0)""")
        original = rows(path)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE staff_users (conflicting_column TEXT)")
        rollback_backup = Path(directory) / "rollback-backup.sqlite3"
        try:
            migrate_with_backup(rollback_backup)
        except sqlite3.OperationalError as exc:
            assert "staff_users" in str(exc)
        else:
            raise AssertionError("A conflicting schema must reject migration")
        assert rows(path) == rows(rollback_backup) == original
        with closing(sqlite3.connect(path)) as conn, conn:
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'").fetchone() is None
            conn.execute("DROP TABLE staff_users")

        migrate_with_backup(backup)
        assert database_ready()
        assert rows(path) == rows(backup) == original
        with closing(sqlite3.connect(backup)) as conn:
            with closing(sqlite3.connect(Path(directory) / "restored.sqlite3")) as restored:
                conn.backup(restored)
        assert rows(Path(directory) / "restored.sqlite3") == original

        read_headers = {"X-MRCARE-Tool-Key": "local-test-read-key"}
        request = {
            "request_type": "callback", "customer_name": "Synthetic caller",
            "phone": "+91 98765-43210", "product_name": "Test unit",
            "issue_description": "Test issue", "customer_confirmed": True,
            "requested_date": "tomorrow", "interaction_id": "TEST-CALL-1",
            "confirmation_text": "Synthetic callback explicitly confirmed",
            "idempotency_key": "invocation-1",
        }
        with TestClient(read_app) as client:
            assert client.get("/readyz").status_code == 200
            assert client.post("/api/voice-lookup", json={"action": "lookup_product"}).status_code == 401
            lookup = {"action": "check_warranty", "model_number": "TEST-MODEL"}
            result = client.post("/api/voice-lookup", headers=read_headers, json=lookup).json()
            assert result["ambiguous"] is True and result["found"] is False
            assert "warranty_confirmed" not in result
            unique = client.post("/api/voice-lookup", headers=read_headers, json={**lookup, "serial_number": "TEST-A"}).json()
            assert unique["found"] is True and unique["serial_number"] == "TEST-A"

        with TestClient(app) as client:
            token = issue_owner_session("test-owner")
            staff = {"Authorization": f"Bearer {token}"}
            assert client.get("/api/demo/requests").status_code == 401
            listing = client.get("/api/demo/requests", headers=staff)
            assert listing.status_code == 200 and listing.headers["Cache-Control"] == "no-store"
            with connection() as conn:
                assert conn.execute("SELECT token_hash FROM staff_sessions").fetchone()[0] != token
            assert client.post("/api/requests", headers=staff, json={**request, "customer_confirmed": False}).status_code == 409
            legacy = {**request, "action": "request_callback"}
            assert client.post("/api/voice-tool", json=legacy).status_code == 401
            assert client.post("/api/voice-tool", headers=read_headers, json={**legacy, "phone": "987654321"}).status_code == 422
            assert client.post("/api/voice-tool", headers=read_headers, json={**legacy, "requested_date": None}).status_code == 422
            assert client.post("/api/voice-tool", headers=read_headers, json={**legacy, "customer_confirmed": False}).status_code == 409
            revoke_owner_sessions("test-owner")
            assert client.get("/api/demo/requests", headers=staff).status_code == 401

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit_request, [request] * 12))
        assert len({result["request"]["id"] for result in results}) == 1
        assert sum(not result.get("duplicate", False) for result in results) == 1
        with TestClient(app) as client:
            retry = client.post("/api/voice-tool", headers=read_headers, json=legacy)
            assert retry.status_code == 200
            assert retry.json()["duplicate"] is True
            assert retry.json()["request"]["id"] == results[0]["request"]["id"]

        write_headers = {"X-MRCARE-Write-Key": "local-test-write-key"}
        voice = {key: value for key, value in request.items() if key != "request_type"}
        voice["action"] = "request_callback"
        with TestClient(write_app) as client:
            collision = client.post("/api/voice-request", headers=write_headers, json={**voice, "issue_description": "Different issue"})
            assert collision.status_code == 409
            newer = client.post("/api/voice-request", headers=write_headers, json={**voice, "idempotency_key": "invocation-2"})
            assert newer.status_code == 200 and newer.json()["duplicate"] is False
        stored = rows(path)
        assert len(stored[-1]) == 3
        assert stored[-1][0] == original[-1][0]
        print("Foundation checks passed (backup, restore, compatibility, staff access, and concurrent retries).")


if __name__ == "__main__":
    main()
