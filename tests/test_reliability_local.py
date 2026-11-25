"""Exercise job recovery, event replay and private recording storage offline."""

import asyncio
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import time

import boto3
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber
from fastapi.testclient import TestClient

os.environ.update(MRCARE_DATABASE_URL="", MRCARE_REDIS_URL="", MRCARE_RECORDINGS_BUCKET="",
                  MRCARE_WORKER_ALLOW_EXTERNAL="0", MRCARE_WHATSAPP_SEND_ENABLED="0",
                  MRCARE_SARVAM_OUTBOUND_ENABLED="0")

from app import call_center, operations, private_recordings, worker
from app.database import begin_write, connection, init_db
from app.main import app
from app.public_api import app as lookup
from app.redis_coordination import rate_allowed
from app.reliability_api import stream
from app.staff_auth import issue_owner_session


def jobs(now):
    with connection() as conn:
        begin_write(conn)
        operations.enqueue(conn, "dispatch-maintenance", 0, "synthetic-worker-restart", due=now)
        operations.enqueue(conn, "dispatch-maintenance", 0, "synthetic-worker-restart", due=now)
        operations.enqueue(conn, "whatsapp-submit", 11, "synthetic-external-disabled", due=now)
    claimed = operations.claim_job(now, operations.INTERNAL_JOBS)
    assert claimed["attempts"] == 1
    assert operations.claim_job(now + operations.LEASE_SECONDS - 1, operations.INTERNAL_JOBS) is None
    restarted = operations.claim_job(now + operations.LEASE_SECONDS, operations.INTERNAL_JOBS)
    assert restarted["id"] == claimed["id"] and restarted["lease_id"] != claimed["lease_id"]
    assert operations.finish_job(claimed, now + 61, "stale-worker") == "lease_lost"
    assert operations.finish_job(restarted, now + 61, "recovered") == "done"
    assert not worker.run_once(external=False, now=now + 61)
    with connection() as conn:
        operations.enqueue(conn, "event-retention", 0, "synthetic-retries", due=now)
    attempt_time = now
    for attempt in range(operations.MAX_ATTEMPTS):
        current = operations.claim_job(attempt_time, operations.INTERNAL_JOBS)
        assert current["attempts"] == attempt + 1
        state = operations.finish_job(current, attempt_time, "synthetic_retry", retry=True)
        if attempt + 1 < operations.MAX_ATTEMPTS:
            assert state == "queued"
            assert operations.claim_job(attempt_time, operations.INTERNAL_JOBS) is None
        else:
            assert state == "dead"
        attempt_time += 301
    operations.schedule_housekeeping(now)
    assert worker.run_once(now=now + 1)


def event_replay(now):
    try:
        with connection() as conn:
            begin_write(conn)
            operations.emit(conn, "case", 99)
            raise RuntimeError("synthetic rollback")
    except RuntimeError:
        pass
    assert operations.events_after(0) == (0, [])
    with connection() as conn:
        begin_write(conn)
        conn.execute("INSERT INTO ops_events(topic,resource_id,created_at) VALUES ('case',1,?)", (now - 8 * 86400,))
        operations.emit(conn, "call", 2)
    head, rows = operations.events_after(0)
    assert [row["resource_id"] for row in rows] == [1, 2]

    async def replay():
        feed = stream(rows[0]["id"], seconds=.05)
        data = [part async for part in feed]
        assert any(f"id: {head}" in part and '"topic":"call"' in part for part in data)
    asyncio.run(replay())
    operations.event_retention(now)
    assert operations.events_after(0) == (head, None)
    assert operations.events_after(head) == (head, [])


def recording(now):
    call_center.ingest_inbound({"app_id": "synthetic-storage", "interaction_id": "20261119/storage",
                               "state": "completed"}, "synthetic-storage")
    call_id = call_center.list_calls(None, 1)[0]["id"]
    data = b"RIFF\0\0\0\0WAVE"
    digest = sha256(data).hexdigest()
    s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="synthetic", aws_secret_access_key="synthetic")
    store = private_recordings.RecordingStore(s3, "synthetic-private-recordings", 7, "synthetic-kms-key")
    stubber = Stubber(s3)
    stubber.add_response("put_object", {}, {
        "Bucket": store.bucket, "Key": ANY, "Body": data, "ContentType": "audio/wav",
        "Metadata": {"sha256": digest}, "ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "synthetic-kms-key",
    })
    with stubber:
        key = store.put(call_id, data, digest)
        assert key.startswith(f"recordings/{call_id}/")
        call_center.store_recording_metadata(call_id, {"content_type": "audio/wav", "sha256": digest, "size_bytes": len(data)},
                                             key, now + 60)
        cached = private_recordings.cached_recording(call_id, now)
        assert cached["storage_key"] == key and "storage_key" not in call_center.call_detail(call_id)["recording"]
        stubber.add_response("get_object", {"Body": StreamingBody(BytesIO(data), len(data))},
                             {"Bucket": store.bucket, "Key": key})
        assert store.get(key, digest, len(data)) == data
        stubber.add_response("delete_object", {}, {"Bucket": store.bucket, "Key": key})
        private_recordings.purge_expired(store, now + 61)
        assert private_recordings.cached_recording(call_id, now) is None
        stubber.assert_no_pending_responses()
    s3.close()


def fallback():
    os.environ["MRCARE_METRICS_KEY"] = "synthetic-metrics-secret-for-local-tests"
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        os.environ["MRCARE_REDIS_URL"] = "redis://127.0.0.1:" + str(unavailable.getsockname()[1]) + "/0"

        async def check():
            assert await rate_allowed("synthetic-unavailable", 1) == (True, False)
            assert await rate_allowed("synthetic-unavailable", 1) == (False, False)

        asyncio.run(check())
        with TestClient(lookup) as client:
            client.post("/api/voice-lookup", json={"action": "lookup_product"})
            assert client.get("/metrics").status_code == 401
            metrics = client.get("/metrics", headers={"Authorization": "Bearer " + os.environ["MRCARE_METRICS_KEY"]})
            assert metrics.status_code == 200
            assert 'mrcare_redis_rate_fallback_total{service="voice-lookup"}' in metrics.text
    os.environ["MRCARE_REDIS_URL"] = ""


def main():
    with TemporaryDirectory() as directory:
        os.environ["MRCARE_DB_PATH"] = str(Path(directory) / "reliability.sqlite3")
        init_db()
        now = int(time.time())
        jobs(now)
        event_replay(now)
        recording(now)
        fallback()
        owner = issue_owner_session("synthetic-reliability-owner")
        with TestClient(app) as client:
            assert client.get("/api/operations/dead-jobs").status_code == 401
            result = client.get("/api/operations/dead-jobs", headers={"Authorization": "Bearer " + owner})
            assert result.json()["items"][0]["attempts"] == operations.MAX_ATTEMPTS
    print("Reliability checks passed (lease recovery, backoff, dead letters, SSE replay, Redis fallback, and encrypted storage contract).")


if __name__ == "__main__":
    main()
