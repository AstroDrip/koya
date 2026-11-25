"""Call records, provider observations and staff-controlled case linkage."""

import json
import time

from .crm import CRMError
from . import crm_channels
from .database import begin_write, connection, now_iso
from .operations import emit

ACTIVE_WINDOW_SECONDS = 15 * 60
TERMINAL = {"completed", "connected", "no_answer", "busy", "failed"}
RANK = {"started": 1, "ended": 2, "completed": 3}


def audit(conn, call_id, owner, action, reason, *, contact_id=None, followup_id=None):
    conn.execute(
        """INSERT INTO voice_audit(call_id,owner_id,contact_id,followup_id,action,reason,occurred_at)
           VALUES (?,?,?,?,?,?,?)""", (call_id, owner, contact_id, followup_id, action, reason, now_iso()),
    )


def require_call(conn, call_id):
    row = conn.execute("SELECT * FROM voice_calls WHERE id=?", (call_id,)).fetchone()
    if not row:
        raise CRMError(404, "Call not found.")
    return row


def _event(conn, app_id, kind, key, digest, now):
    previous = conn.execute(
        "SELECT payload_hash FROM voice_event_inbox WHERE app_id=? AND kind=? AND event_key=?",
        (app_id, kind, key),
    ).fetchone()
    if previous:
        if previous["payload_hash"] != digest:
            raise CRMError(409, "A different event already used this delivery identity.")
        return False
    conn.execute("INSERT INTO voice_event_inbox VALUES (?,?,?,?,?)", (app_id, kind, key, digest, now))
    return True


def ingest_inbound(event, digest):
    now = int(time.time())
    app_id, interaction = event["app_id"], event["interaction_id"]
    state = event["state"]
    source = "post_call_webhook" if state == "completed" else "sarvam_conversation_hook"
    with connection() as conn:
        begin_write(conn)
        received = _event(conn, app_id, state, interaction, digest, now)
        call = conn.execute("SELECT * FROM voice_calls WHERE app_id=? AND interaction_id=?",
                            (app_id, interaction)).fetchone()
        if call and call["direction"] != "inbound":
            raise CRMError(409, "Interaction direction conflicts with the existing call.")
        if not received:
            return {"ok": True, "duplicate": True}
        if not call:
            created = conn.execute(
                """INSERT INTO voice_calls(app_id,deployment_id,app_version,direction,interaction_id,
                     caller_number,agent_number,state,started_at,ended_at,observed_at,source,
                     duration_seconds,transcript_reported,reported_outcome)
                   VALUES (?,?,?,'inbound',?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                (app_id, event.get("deployment_id"), event.get("app_version"), interaction, event.get("caller_number"),
                 event.get("agent_number"), state, event.get("started_at"), event.get("ended_at"), now, source,
                 event.get("duration"), event.get("transcript_reported", False), event.get("reported_outcome")),
            ).fetchone()[0]
            emit(conn, "call", created)
        elif RANK[state] > RANK[call["state"]]:
            conn.execute(
                """UPDATE voice_calls SET state=?,source=?,observed_at=?,
                   started_at=coalesce(?,started_at),ended_at=coalesce(?,ended_at),
                   caller_number=coalesce(?,caller_number),agent_number=coalesce(?,agent_number),
                   deployment_id=coalesce(?,deployment_id),app_version=coalesce(?,app_version),
                   duration_seconds=?,transcript_reported=?,reported_outcome=? WHERE id=?""",
                (state, source, now, event.get("started_at"), event.get("ended_at"),
                 event.get("caller_number"), event.get("agent_number"), event.get("deployment_id"), event.get("app_version"),
                 event.get("duration"), event.get("transcript_reported", False), event.get("reported_outcome"), call["id"]),
            )
            if call["case_id"] is not None:
                crm_channels.release(conn, call["case_id"], f"voice:{call['id']}")
            emit(conn, "call", call["id"])
        elif state == "started":
            conn.execute("UPDATE voice_calls SET started_at=coalesce(started_at,?) WHERE id=?",
                         (event.get("started_at"), call["id"]))
    return {"ok": True, "duplicate": False}


def _mask(number):
    return f"••••{number[-4:]}" if number else None


def call_view(row, now):
    call = dict(row)
    for field in ("caller_number", "agent_number"):
        call[field] = _mask(call[field])
    call["media_allowed"] = bool(call["media_allowed"] and call["media_until"] and call["media_until"] > now)
    call["observation"] = (
        ("start_observed" if (call["started_at"] or call["observed_at"]) >= now - ACTIVE_WINDOW_SECONDS else "stale")
        if call["state"] == "started" else "last_verified"
    )
    return call


def list_calls(before, limit):
    now = int(time.time())
    with connection(read_only=True) as conn:
        rows = conn.execute("SELECT * FROM voice_calls WHERE (CAST(? AS BIGINT) IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                            (before, before, limit))
        return [call_view(row, now) for row in rows]


def call_detail(call_id):
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        call = call_view(require_call(conn, call_id), int(time.time()))
        meta = conn.execute(
            """SELECT call_id,content_type,size_bytes,sha256,checked_at,retention_until
               FROM voice_recording_metadata WHERE call_id=?""", (call_id,),
        ).fetchone()
        escalations = conn.execute("SELECT * FROM voice_escalations WHERE call_id=? ORDER BY id", (call_id,))
        return {**call, "recording": dict(meta) if meta else None, "escalations": [dict(row) for row in escalations]}


def link_case(call_id, case_id, reason, owner):
    with connection() as conn:
        begin_write(conn)
        call = require_call(conn, call_id)
        case = conn.execute("SELECT customer_id FROM crm_cases WHERE id=?", (case_id,)).fetchone()
        if not case:
            raise CRMError(404, "Case not found.")
        if call["case_id"] is not None and call["case_id"] != case_id:
            raise CRMError(409, "This call is already linked. Review the association before making a correction.")
        if call["case_id"] is None:
            now = int(time.time())
            expires = (call["started_at"] or call["observed_at"]) + ACTIVE_WINDOW_SECONDS
            if call["state"] == "started" and expires > now:
                crm_channels.claim(conn, case_id, "voice", f"voice:{call_id}", now, expires)
            conn.execute("UPDATE voice_calls SET case_id=?,customer_id=? WHERE id=?",
                         (case_id, case["customer_id"], call_id))
            audit(conn, call_id, owner, "case_linked", reason)
            emit(conn, "call", call_id)
    return call_detail(call_id)


def set_media_permission(call_id, allowed, until, evidence, owner):
    now = int(time.time())
    if allowed and (until is None or not now < until <= now + 30 * 86400):
        raise CRMError(422, "Provide a media access expiry within the next 30 days.")
    with connection() as conn:
        begin_write(conn)
        call = require_call(conn, call_id)
        if allowed and (call["state"] not in TERMINAL or not call["interaction_id"] or call["case_id"] is None):
            raise CRMError(409, "Link a completed conversation to its case before approving media access.")
        conn.execute("UPDATE voice_calls SET media_allowed=?,media_until=? WHERE id=?", (allowed, until if allowed else None, call_id))
        audit(conn, call_id, owner, "media_authorized" if allowed else "media_revoked", evidence)


def authorize_media(call_id, kind, owner):
    with connection() as conn:
        begin_write(conn)
        call = require_call(conn, call_id)
        if not (call["interaction_id"] and call["media_allowed"] and (call["media_until"] or 0) > int(time.time())):
            raise CRMError(403, "Media access is unavailable.")
        audit(conn, call_id, owner, f"{kind}_requested", "Owner requested access to provider media.")
        return call["app_id"], call["interaction_id"], call["media_until"]


def media_still_authorized(call_id, initial_permission):
    with connection(read_only=True) as conn:
        call = require_call(conn, call_id)
        if (not call["media_allowed"] or (call["media_until"] or 0) <= int(time.time())
                or call["media_until"] != initial_permission):
            raise CRMError(403, "Media access is unavailable.")


def store_recording_metadata(call_id, info, storage_key=None, retention_until=None):
    with connection() as conn:
        conn.execute(
            """INSERT INTO voice_recording_metadata(call_id,content_type,size_bytes,sha256,checked_at,storage_key,retention_until)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(call_id) DO UPDATE SET content_type=excluded.content_type,
                   size_bytes=excluded.size_bytes,sha256=excluded.sha256,checked_at=excluded.checked_at,
                   storage_key=excluded.storage_key,retention_until=excluded.retention_until""",
            (call_id, info["content_type"], info["size_bytes"], info["sha256"], now_iso(), storage_key, retention_until),
        )


def add_escalation(app_id, interaction, category):
    with connection() as conn:
        begin_write(conn)
        call = conn.execute("SELECT id FROM voice_calls WHERE app_id=? AND interaction_id=?",
                            (app_id, interaction)).fetchone()
        if not call:
            raise CRMError(404, "Call not found.")
        row = conn.execute(
            """INSERT INTO voice_escalations(call_id,category,status,requested_at) VALUES (?,?,'requested',?)
               ON CONFLICT(call_id,category) DO NOTHING RETURNING id""",
            (call["id"], category, now_iso()),
        ).fetchone()
        if row:
            emit(conn, "escalation", row[0])
            return {"escalation_id": row[0], "duplicate": False}
        existing = conn.execute("SELECT id FROM voice_escalations WHERE call_id=? AND category=?", (call["id"], category)).fetchone()
        return {"escalation_id": existing[0], "duplicate": True}


def resolve_escalation(escalation_id, resolution, owner):
    with connection() as conn:
        begin_write(conn)
        row = conn.execute(
            """UPDATE voice_escalations SET status='resolved',resolved_at=?,resolved_by=?,resolution=?
               WHERE id=? AND status='requested' RETURNING call_id""", (now_iso(), owner, resolution, escalation_id),
        ).fetchone()
        if not row:
            raise CRMError(404, "Open escalation not found.")
        audit(conn, row["call_id"], owner, "escalation_resolved", resolution)
        emit(conn, "escalation", escalation_id)


def list_escalations():
    with connection(read_only=True) as conn:
        return [dict(row) for row in conn.execute(
            """SELECT e.id,e.call_id,e.category,e.requested_at,c.case_id FROM voice_escalations e
               JOIN voice_calls c ON c.id=e.call_id WHERE e.status='requested' ORDER BY e.id LIMIT 100""",
        )]
