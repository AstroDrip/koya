"""Consent-checked Happy Call tasks and authenticated outcome reconciliation."""

from datetime import datetime, timezone
import re
import time

from . import crm_channels
from .call_center import ACTIVE_WINDOW_SECONDS, audit, require_call
from .crm import CRMError
from .database import begin_write, connection, now_iso
from .operations import emit, enqueue
from .finance import customer_estimate
from .sarvam_voice import OutboundRejected


def set_permission(contact_id, allowed, evidence, owner):
    with connection() as conn:
        begin_write(conn)
        if not conn.execute("SELECT 1 FROM crm_contacts WHERE id=?", (contact_id,)).fetchone():
            raise CRMError(404, "Verified contact not found.")
        row = conn.execute(
            """INSERT INTO voice_permissions(contact_id,permitted,at,evidence,owner_id,version)
               VALUES (?,?,?,?,?,1) ON CONFLICT(contact_id) DO UPDATE SET
               permitted=excluded.permitted,at=excluded.at,evidence=excluded.evidence,
               owner_id=excluded.owner_id,version=voice_permissions.version+1 RETURNING version""",
            (contact_id, allowed, now_iso(), evidence, owner),
        ).fetchone()
        if not allowed:
            conn.execute("UPDATE voice_followups SET state='cancelled',updated_at=? WHERE contact_id=? AND state='queued'",
                         (int(time.time()), contact_id))
        audit(conn, None, owner, "outbound_permission_granted" if allowed else "outbound_permission_revoked",
              evidence, contact_id=contact_id)
        return {"permitted": allowed, "version": row[0]}


def _task(conn, task_id):
    row = conn.execute("SELECT * FROM voice_followups WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise CRMError(404, "Happy Call task not found.")
    return row


def _context(conn, case_id, contact_id, now):
    ctx = conn.execute(
        """SELECT c.id AS case_id,c.reference,v.mobile,n.version AS permission_version,
                  p.id AS appointment_id,p.version AS appointment_version,p.starts_at,p.ends_at,t.name AS technician_name
           FROM crm_cases c JOIN crm_contacts v ON v.customer_id=c.customer_id
           JOIN voice_permissions n ON n.contact_id=v.id AND n.permitted=1
           JOIN technician_assignments a ON a.case_id=c.id AND a.status IN ('accepted','en_route','in_progress','awaiting_parts')
           JOIN dispatch_appointments p ON p.assignment_id=a.id AND p.status='confirmed'
           JOIN technicians t ON t.id=a.technician_id
           WHERE c.id=? AND v.id=? AND c.status='accepted' AND p.ends_at>?""",
        (case_id, contact_id, now),
    ).fetchone()
    if not ctx:
        raise CRMError(409, "A verified, permitted contact and a confirmed visit are required.")
    return ctx


def _current_context(conn, task, now):
    ctx = _context(conn, task["case_id"], task["contact_id"], now)
    for field in ("permission_version", "appointment_id", "appointment_version"):
        if task[field] != ctx[field]:
            raise CRMError(409, "The confirmed visit or contact permission changed. Review a new task.")
    return ctx


def queue_task(case_id, contact_id, operation_key, retry_of, owner):
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        existing = conn.execute("SELECT * FROM voice_followups WHERE operation_key=?", (operation_key,)).fetchone()
        if existing:
            if (case_id, contact_id, retry_of) != (existing["case_id"], existing["contact_id"], existing["retry_of"]):
                raise CRMError(409, "Operation key is already associated with another Happy Call.")
            return dict(existing)
        ctx = _context(conn, case_id, contact_id, now)
        if retry_of is not None:
            parent = _task(conn, retry_of)
            if (case_id, contact_id) != (parent["case_id"], parent["contact_id"]) or parent["state"] not in {"no_answer", "busy", "failed"}:
                raise CRMError(409, "This attempt is ineligible for retry.")
            if conn.execute("SELECT 1 FROM voice_followups WHERE retry_of=?", (retry_of,)).fetchone():
                raise CRMError(409, "This attempt already has a retry.")
        if conn.execute(
            """SELECT 1 FROM voice_followups WHERE case_id=? AND contact_id=?
               AND state IN ('queued','dispatching','submitted','uncertain')""", (case_id, contact_id),
        ).fetchone():
            raise CRMError(409, "A Happy Call is already pending for this case and contact.")
        task = conn.execute(
            """INSERT INTO voice_followups(operation_key,case_id,contact_id,appointment_id,appointment_version,
                        permission_version,requested_by,state,retry_of,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'queued',?,?,?) RETURNING *""",
            (operation_key, case_id, contact_id, ctx["appointment_id"], ctx["appointment_version"],
             ctx["permission_version"], owner, retry_of, now, now),
        ).fetchone()
        audit(conn, None, owner, "happy_call_queued", "Verified contact and confirmed visit checked.",
              contact_id=contact_id, followup_id=task["id"])
        emit(conn, "followup", task["id"])
        return dict(task)


def get_task(task_id):
    with connection(read_only=True) as conn:
        return dict(_task(conn, task_id))


def list_tasks(before, limit):
    with connection(read_only=True) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM voice_followups WHERE (CAST(? AS BIGINT) IS NULL OR id<?) ORDER BY id DESC LIMIT ?", (before, before, limit),
        )]


def brief(task_id):
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        task = _task(conn, task_id)
        now = int(time.time())
        ctx = _current_context(conn, task, now)
        return {
            "reference": ctx["reference"], "technician_name": ctx["technician_name"],
            "visit": {"status": "confirmed", "starts_at": ctx["starts_at"], "ends_at": ctx["ends_at"]},
            "approved_estimate": customer_estimate(conn, ctx["case_id"], now),
        }


def _transition(conn, task, state, now, owner, reason, call_id=None):
    conn.execute("UPDATE voice_followups SET state=?,updated_at=?,call_id=coalesce(?,call_id) WHERE id=?",
                 (state, now, call_id, task["id"]))
    audit(conn, call_id or task["call_id"], owner, "happy_call_" + state, reason,
          contact_id=task["contact_id"], followup_id=task["id"])
    emit(conn, "followup", task["id"])


def submit_task(task_id, provider, owner):
    if provider is None or provider.outbound is None:
        raise CRMError(503, "Outbound dialing is disabled.")
    now = int(time.time())
    failure = None
    with connection() as conn:
        begin_write(conn)
        task = _task(conn, task_id)
        if task["state"] != "queued":
            raise CRMError(409, "This task is not queued for dialing.")
        try:
            ctx = _current_context(conn, task, now)
        except CRMError as exc:
            failure = exc
            _transition(conn, task, "cancelled", now, owner, "Visit or permission changed before dialing.")
        else:
            crm_channels.claim(conn, task["case_id"], "voice", f"voice-followup:{task_id}",
                               now, now + ACTIVE_WINDOW_SECONDS)
            _transition(conn, task, "dispatching", now, owner, "Owner authorized submission to the provider.")
    if failure:
        raise failure

    phone = "+91" + ctx["mobile"]
    try:
        attempt = provider.dial(phone)
    except OutboundRejected:
        state = "failed"
    except CRMError:
        state = "uncertain"
    else:
        state = "submitted"

    with connection() as conn:
        begin_write(conn)
        task = _task(conn, task_id)
        if task["state"] != "dispatching":
            raise CRMError(409, "The dial attempt requires reconciliation.")
        if state == "submitted":
            settings = provider.outbound
            call_id = conn.execute(
                """INSERT INTO voice_calls(app_id,app_version,direction,attempt_id,caller_number,agent_number,
                             state,observed_at,source,case_id,customer_id)
                   SELECT ?,?,'outbound',?,?,?,'submitted',?,'sarvam_submission',c.id,c.customer_id
                   FROM crm_cases c WHERE c.id=? RETURNING id""",
                (settings.app_id, settings.version, attempt, phone, settings.number, now, task["case_id"]),
            ).fetchone()[0]
            _transition(conn, task, state, now, owner, "Provider returned an attempt identifier.", call_id)
            enqueue(conn, "voice-reconcile", task_id, f"voice-reconcile:{task_id}", due=now + 60)
        else:
            _transition(conn, task, state, now, owner, "Provider rejected submission." if state == "failed"
                        else "Submission outcome is unknown; do not redial automatically.")
            if state == "failed":
                crm_channels.release(conn, task["case_id"], f"voice-followup:{task_id}")
        return dict(_task(conn, task_id))


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value)
        return int((date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def reconcile_task(task_id, provider, owner):
    if not provider:
        raise CRMError(503, "Sarvam Analytics is unavailable.")
    now = int(time.time())
    with connection(read_only=True) as conn:
        task = _task(conn, task_id)
        if task["state"] != "submitted":
            return dict(task)
        call = require_call(conn, task["call_id"])
    result = provider.attempt(call["app_id"], call["attempt_id"], call["observed_at"], now)
    if not result or result.get("channel_direction") != "outbound":
        return get_task(task_id)
    outcome = result.get("connectivity_status")
    start, end = (_timestamp(result.get(field)) for field in ("start_datetime", "end_datetime"))
    if outcome not in {"connected", "no_answer", "busy", "failed"} or (outcome == "connected" and end is None):
        return get_task(task_id)
    if result.get("user_contact") != call["caller_number"]:
        raise CRMError(502, "Sarvam attempt did not match the expected contact.")
    if start is not None and end is not None and end < start:
        raise CRMError(502, "Sarvam returned inconsistent call times.")
    interaction = result.get("interaction_id") if outcome == "connected" else None
    if interaction is not None and not re.fullmatch(r"[0-9]{8}/[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", str(interaction)):
        raise CRMError(502, "Sarvam returned an unusable interaction identifier.")
    duration = result.get("duration_in_seconds")
    if type(duration) not in (int, float) or not 0 <= duration <= 86400:
        duration = None
    failure = result.get("failure_reason")
    if isinstance(failure, str) and failure != "NO_FAILURE_REASON":
        failure = re.sub(r"\+?[0-9][0-9 ()-]{7,18}[0-9]", "[redacted]", failure)[:300]
    else:
        failure = None
    with connection() as conn:
        begin_write(conn)
        task = _task(conn, task_id)
        if task["state"] != "submitted":
            return dict(task)
        if interaction and conn.execute(
            "SELECT 1 FROM voice_calls WHERE app_id=? AND interaction_id=? AND id<>?",
            (call["app_id"], interaction, call["id"]),
        ).fetchone():
            raise CRMError(409, "The provider interaction is already associated with another call.")
        conn.execute(
            """UPDATE voice_calls SET state=?,interaction_id=?,started_at=?,ended_at=?,observed_at=?,
                        source='sarvam_analytics',duration_seconds=?,failure_reason=? WHERE id=?""",
            (outcome, interaction, start, end, now, duration, failure, call["id"]),
        )
        _transition(conn, task, outcome, now, owner, "Outcome observed in authenticated Sarvam Analytics.")
        crm_channels.release(conn, task["case_id"], f"voice-followup:{task_id}")
        return dict(_task(conn, task_id))


def mark_informed(task_id, disposition, owner):
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        task = _task(conn, task_id)
        if task["state"] != "connected":
            raise CRMError(409, "Review a connected attempt before marking the customer informed.")
        if task["informed_at"]:
            return dict(task)
        conn.execute("UPDATE voice_followups SET informed_at=?,disposition=?,updated_at=? WHERE id=?",
                     (now, disposition, now, task_id))
        audit(conn, task["call_id"], owner, "customer_informed", disposition, followup_id=task_id)
        emit(conn, "followup", task_id)
        return dict(_task(conn, task_id))


def recover_uncertain(task_id, evidence, owner):
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        task = _task(conn, task_id)
        if task["state"] == "dispatching" and task["updated_at"] <= now - 120:
            _transition(conn, task, "uncertain", now, owner, "A pending provider submission lost its acknowledgement.")
        elif task["state"] == "uncertain":
            _transition(conn, task, "cancelled", now, owner, evidence)
            crm_channels.release(conn, task["case_id"], f"voice-followup:{task_id}")
        else:
            raise CRMError(409, "This task is not eligible for manual review.")
        return dict(_task(conn, task_id))
