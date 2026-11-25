"""Optional encrypted S3 storage for consented recordings."""

from functools import lru_cache
from hashlib import sha256
import os
from uuid import uuid4

from .crm import CRMError
from .database import begin_write, connection
from .sarvam_voice import MAX_RECORDING


class RecordingStore:
    def __init__(self, client, bucket, days, kms_key=None):
        self.client = client
        self.bucket = bucket
        self.seconds = days * 86400
        self.encryption = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms_key} if kms_key else {
            "ServerSideEncryption": "AES256",
        }

    def put(self, call_id, data, digest):
        from botocore.exceptions import BotoCoreError, ClientError

        key = f"recordings/{call_id}/{uuid4().hex}.wav"
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType="audio/wav",
                                   Metadata={"sha256": digest}, **self.encryption)
        except (ClientError, BotoCoreError):
            raise CRMError(502, "Private recording storage is unavailable.") from None
        return key

    def get(self, key, digest, size):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            with response["Body"] as body:
                data = body.read(MAX_RECORDING + 1)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise CRMError(502, "Private recording storage is unavailable.") from None
        except BotoCoreError:
            raise CRMError(502, "Private recording storage is unavailable.") from None
        if size != len(data) or len(data) > MAX_RECORDING or sha256(data).hexdigest() != digest:
            raise CRMError(502, "The stored recording failed its integrity check.")
        return data

    def delete(self, key):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError):
            raise CRMError(502, "Private recording storage is unavailable.") from None


@lru_cache(maxsize=2)
def _store(bucket, days, endpoint, region, kms):
    import boto3
    from botocore.config import Config

    client = boto3.client("s3", endpoint_url=endpoint, region_name=region,
                          config=Config(connect_timeout=3, read_timeout=8, retries={"total_max_attempts": 2}))
    return RecordingStore(client, bucket, days, kms)


def configured_recordings():
    bucket = os.getenv("MRCARE_RECORDINGS_BUCKET", "").strip()
    raw = os.getenv("MRCARE_RECORDINGS_RETENTION_DAYS", "").strip()
    if not bucket or not raw.isascii() or not raw.isdigit() or len(raw) > 2 or not 1 <= int(raw) <= 90:
        return None
    endpoint = os.getenv("MRCARE_RECORDINGS_ENDPOINT", "").strip() or None
    if endpoint is not None and not endpoint.startswith("https://"):
        raise CRMError(503, "Private recordings storage requires HTTPS.")
    return _store(bucket, int(raw), endpoint, os.getenv("AWS_REGION") or None, os.getenv("MRCARE_RECORDINGS_KMS_KEY") or None)


def cached_recording(call_id, now):
    with connection(read_only=True) as conn:
        row = conn.execute(
            """SELECT storage_key,sha256,size_bytes FROM voice_recording_metadata
               WHERE call_id=? AND storage_key IS NOT NULL AND retention_until>?""", (call_id, now),
        ).fetchone()
        return dict(row) if row else None


def purge_expired(store, now):
    if store is None:
        return
    with connection(read_only=True) as conn:
        expired = conn.execute(
            """SELECT m.call_id,m.storage_key FROM voice_recording_metadata m JOIN voice_calls c ON c.id=m.call_id
               WHERE m.storage_key IS NOT NULL AND (m.retention_until<=? OR c.media_allowed=0 OR c.media_until<=?)
               ORDER BY m.retention_until LIMIT 100""", (now, now),
        ).fetchall()
    for call_id, key in expired:
        store.delete(key)
        with connection() as conn:
            begin_write(conn)
            conn.execute("UPDATE voice_recording_metadata SET storage_key=NULL,retention_until=NULL WHERE call_id=? AND storage_key=?",
                         (call_id, key))
