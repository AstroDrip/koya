"""Technician eligibility, assignments and appointments."""

from datetime import datetime, time as day_time, timedelta
import hashlib
import secrets
import time

from .crm import CRMError, KOLKATA
from .database import begin_write, connection, now_iso
from .operations import emit

ACTIVE = "'offered','accepted','en_route','in_progress','awaiting_parts'"
BOOKING_HORIZON = 180 * 86400
WORKFLOW = {
    "offered": {"accepted", "declined"},
    "accepted": {"en_route"},
    "en_route": {"in_progress"},
    "in_progress": {"awaiting_parts", "completed"},
    "awaiting_parts": {"in_progress"},
}


def _audit(conn, case_id, event, reason, *, owner=None, technician=None):
    conn.execute(
        """INSERT INTO dispatch_audit(case_id,owner_id,technician_id,event,reason,occurred_at)
           VALUES (?,?,?,?,?,?)""", (case_id, owner, technician, event, reason, now_iso()),
    )


def _notify(conn, case_id, kind, technician=None):
    conn.execute("INSERT INTO dispatch_notifications(technician_id,case_id,kind,created_at) VALUES (?,?,?,?)",
                 (technician, case_id, kind, now_iso()))
    emit(conn, "dispatch", case_id)


def _technician(conn, technician_id):
    row = conn.execute("SELECT * FROM technicians WHERE id=?", (technician_id,)).fetchone()
    if not row:
        raise CRMError(404, "Technician not found.")
    return row


def _working(conn, technician_id, start, end):
    local = datetime.fromtimestamp(start, KOLKATA)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if end > int((day + timedelta(days=1)).timestamp()):
        return False
    minutes = tuple((stamp - int(day.timestamp())) / 60 for stamp in (start, end))
    hours = conn.execute(
        """SELECT 1 FROM technician_hours WHERE technician_id=? AND weekday=?
           AND start_minute<=? AND end_minute>=? AND end_minute>?""",
        (technician_id, local.weekday(), *minutes, minutes[0]),
    ).fetchone()
    leave = conn.execute(
        "SELECT 1 FROM technician_leave WHERE technician_id=? AND starts_at<? AND ends_at>?",
        (technician_id, max(start + 1, end), start),
    ).fetchone()
    return hours is not None and leave is None


def save_technician(code, data, owner):
    with connection() as conn:
        begin_write(conn)
        technician_id = conn.execute(
            """INSERT INTO technicians(code,name,active,capacity) VALUES (?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET name=excluded.name,active=excluded.active,capacity=excluded.capacity
               RETURNING id""", (code, data["name"], data["active"], data["capacity"]),
        ).fetchone()[0]
        active = conn.execute(
            f"""SELECT a.case_id,r.pincode,r.skill FROM technician_assignments a
                JOIN dispatch_requirements r ON r.case_id=a.case_id
                WHERE a.technician_id=? AND a.status IN ({ACTIVE})""", (technician_id,),
        ).fetchall()
        if len(active) > data["capacity"] or any(
            not data["active"] or job["pincode"] not in data["pincodes"] or job["skill"] not in data["skills"]
            for job in active
        ):
            raise CRMError(409, "Release or reassign affected jobs before changing this profile.")
        for table in ("technician_coverage", "technician_skills", "technician_hours"):
            conn.execute(f"DELETE FROM {table} WHERE technician_id=?", (technician_id,))
        conn.executemany("INSERT INTO technician_coverage VALUES (?,?)", ((technician_id, pin) for pin in sorted(data["pincodes"])))
        conn.executemany("INSERT INTO technician_skills VALUES (?,?)", ((technician_id, skill) for skill in sorted(data["skills"])))
        conn.executemany("INSERT INTO technician_hours VALUES (?,?,?,?)", (
            (technician_id, row["weekday"], row["start_minute"], row["end_minute"]) for row in data["hours"]
        ))
        booked = conn.execute(
            """SELECT p.starts_at,p.ends_at FROM dispatch_appointments p JOIN technician_assignments a ON a.id=p.assignment_id
               WHERE a.technician_id=? AND p.status='confirmed' AND p.ends_at>?""", (technician_id, int(time.time())),
        )
        if any(not _working(conn, technician_id, row["starts_at"], row["ends_at"]) for row in booked):
            raise CRMError(409, "Reschedule affected appointments before changing published hours.")
        if not data["active"]:
            conn.execute("DELETE FROM technician_sessions WHERE technician_id=?", (technician_id,))
        _audit(conn, None, "technician_profile_saved", "Owner updated eligibility and working hours.",
               owner=owner, technician=technician_id)
        return dict(_technician(conn, technician_id))


def list_technicians():
    with connection(read_only=True) as conn:
        return [dict(row) for row in conn.execute(f"""SELECT t.*,
            (SELECT count(*) FROM technician_assignments a WHERE a.technician_id=t.id AND a.status IN ({ACTIVE})) AS workload
            FROM technicians t ORDER BY t.code""")]


def technician_detail(technician_id):
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        result = dict(_technician(conn, technician_id))
        result["pincodes"] = [row[0] for row in conn.execute(
            "SELECT pincode FROM technician_coverage WHERE technician_id=? ORDER BY pincode", (technician_id,),
        )]
        result["skills"] = [row[0] for row in conn.execute(
            "SELECT skill FROM technician_skills WHERE technician_id=? ORDER BY skill", (technician_id,),
        )]
        result["hours"] = [dict(row) for row in conn.execute(
            "SELECT weekday,start_minute,end_minute FROM technician_hours WHERE technician_id=? ORDER BY weekday,start_minute",
            (technician_id,),
        )]
        result["leave"] = [dict(row) for row in conn.execute(
            "SELECT id,starts_at,ends_at,reason FROM technician_leave WHERE technician_id=? ORDER BY starts_at",
            (technician_id,),
        )]
        return result


def unassigned_cases(limit):
    with connection(read_only=True) as conn:
        return [dict(row) for row in conn.execute(
            f"""SELECT c.id,c.reference,c.priority,r.pincode,r.skill FROM crm_cases c
                LEFT JOIN dispatch_requirements r ON r.case_id=c.id
                WHERE c.status='accepted' AND NOT EXISTS (
                    SELECT 1 FROM technician_assignments a WHERE a.case_id=c.id
                    AND a.status IN ({ACTIVE},'completed')
                )
                ORDER BY (c.priority='urgent') DESC,c.id LIMIT ?""", (limit,),
        )]


def add_leave(technician_id, start, end, reason, owner):
    if start >= end or end <= int(time.time()):
        raise CRMError(422, "Provide a future or ongoing leave interval.")
    with connection() as conn:
        begin_write(conn)
        _technician(conn, technician_id)
        if _booking_conflict(conn, technician_id, start, end):
            raise CRMError(409, "Reschedule confirmed appointments before adding leave.")
        leave_id = conn.execute(
            "INSERT INTO technician_leave(technician_id,starts_at,ends_at,reason) VALUES (?,?,?,?) RETURNING id",
            (technician_id, start, end, reason),
        ).fetchone()[0]
        _audit(conn, None, "technician_leave_added", reason, owner=owner, technician=technician_id)
        return leave_id


def _session_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_session(technician_id, owner):
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with connection() as conn:
        begin_write(conn)
        if not _technician(conn, technician_id)["active"]:
            raise CRMError(409, "Technician account is inactive.")
        conn.execute("DELETE FROM technician_sessions WHERE expires_at<=?", (now,))
        conn.execute("INSERT INTO technician_sessions VALUES (?,?,?)", (_session_hash(token), technician_id, now + 3600))
        _audit(conn, None, "technician_session_issued", "Owner provisioned a one-hour session.", owner=owner, technician=technician_id)
    return token


def revoke_sessions(technician_id, owner):
    with connection() as conn:
        _technician(conn, technician_id)
        conn.execute("DELETE FROM technician_sessions WHERE technician_id=?", (technician_id,))
        _audit(conn, None, "technician_sessions_revoked", "Owner revoked technician sessions.", owner=owner, technician=technician_id)


def technician_session(token):
    with connection(read_only=True) as conn:
        row = conn.execute(
            """SELECT t.id,t.code,t.name FROM technician_sessions s JOIN technicians t ON t.id=s.technician_id
               WHERE s.verifier=? AND s.expires_at>? AND t.active=1""", (_session_hash(token), int(time.time())),
        ).fetchone()
        return dict(row) if row else None


def end_session(token, technician_id):
    with connection() as conn:
        conn.execute("DELETE FROM technician_sessions WHERE verifier=? AND technician_id=?",
                     (_session_hash(token), technician_id))


def _assignment(conn, assignment_id, technician=None):
    result = conn.execute("SELECT * FROM technician_assignments WHERE id=?", (assignment_id,)).fetchone()
    if not result or (technician is not None and result["technician_id"] != technician):
        raise CRMError(404, "Assignment not found.")
    return result


def _set_status(conn, job, status, reason, *, owner=None, technician=None):
    conn.execute("UPDATE technician_assignments SET status=?,version=version+1 WHERE id=?", (status, job["id"]))
    if status in {"released", "completed"}:
        appointment_status = "completed" if status == "completed" else "cancelled"
        conn.execute("UPDATE dispatch_appointments SET status=?,version=version+1 WHERE assignment_id=? AND status='confirmed'",
                     (appointment_status, job["id"]))
    _audit(conn, job["case_id"], f"assignment_{status}", reason, owner=owner, technician=technician)
    if status in {"accepted", "declined", "expired", "completed"}:
        _notify(conn, job["case_id"], f"assignment_{status}")
    elif status == "released":
        _notify(conn, job["case_id"], "assignment_released", job["technician_id"])
    return dict(_assignment(conn, job["id"]))


def _expire_offers(conn, now):
    offers = conn.execute("SELECT * FROM technician_assignments WHERE status='offered' AND offered_until<=?", (now,)).fetchall()
    for job in offers:
        _set_status(conn, job, "expired", "Offer acceptance window expired.")
    for job in offers:
        requirements = conn.execute("SELECT * FROM dispatch_requirements WHERE case_id=?", (job["case_id"],)).fetchone()
        _dispatch(conn, job["case_id"], requirements["pincode"], requirements["skill"], None, now, None, "Reassignment after timeout.")


def _eligible(conn, pincode, skill, now):
    rows = conn.execute(
        f"""SELECT t.id,t.code,t.capacity,count(a.id) AS workload FROM technicians t
            JOIN technician_coverage c ON c.technician_id=t.id AND c.pincode=?
            JOIN technician_skills s ON s.technician_id=t.id AND s.skill=?
            LEFT JOIN technician_assignments a ON a.technician_id=t.id AND a.status IN ({ACTIVE})
            WHERE t.active=1 GROUP BY t.id ORDER BY workload,t.code""", (pincode, skill),
    ).fetchall()
    return [row for row in rows if row["workload"] < row["capacity"] and _working(conn, row["id"], now, now)]


def _dispatch(conn, case_id, pincode, skill, owner, now, selection, reason):
    active = conn.execute(f"SELECT * FROM technician_assignments WHERE case_id=? AND status IN ({ACTIVE})", (case_id,)).fetchone()
    if active:
        if selection is not None:
            raise CRMError(409, "Release the active assignment before a manual override.")
        if not conn.execute("SELECT 1 FROM dispatch_requirements WHERE case_id=? AND pincode=? AND skill=?",
                            (case_id, pincode, skill)).fetchone():
            raise CRMError(409, "Release the active assignment before changing eligibility requirements.")
        return {"state": "assigned", "assignment": dict(active)}
    _save_requirements(conn, case_id, pincode, skill)
    candidates = _eligible(conn, pincode, skill, now)
    if selection is not None:
        candidates = [row for row in candidates if row["id"] == selection]
        if not candidates:
            raise CRMError(409, "Selected technician does not meet assignment requirements.")
    else:
        declined = conn.execute(
            "SELECT technician_id FROM technician_assignments WHERE case_id=? AND status IN ('declined','expired')", (case_id,),
        )
        skipped = {row[0] for row in declined}
        candidates = [row for row in candidates if row["id"] not in skipped]
    if not candidates:
        conn.execute(
            """INSERT INTO dispatch_notifications(case_id,kind,created_at)
               SELECT ?,'unassigned',? WHERE NOT EXISTS (
                   SELECT 1 FROM dispatch_notifications WHERE case_id=? AND kind='unassigned' AND acknowledged_at IS NULL)""",
            (case_id, now_iso(), case_id),
        )
        _audit(conn, case_id, "assignment_unavailable", "No eligible technician available.", owner=owner)
        return {"state": "unassigned", "assignment": None}
    technician = candidates[0]["id"]
    assignment_id = conn.execute(
        """INSERT INTO technician_assignments(case_id,technician_id,status,offered_until,created_at)
           VALUES (?,?,'offered',?,?) RETURNING id""", (case_id, technician, now + 30 * 60, now),
    ).fetchone()[0]
    _notify(conn, case_id, "assignment_offered", technician)
    _audit(conn, case_id, "assignment_offered", reason, owner=owner, technician=technician)
    return {"state": "assigned", "assignment": dict(_assignment(conn, assignment_id))}


def assign(case_id, pincode, skill, owner, selection=None, reason="Automatic eligibility match."):
    with connection() as conn:
        begin_write(conn)
        case = conn.execute("SELECT status FROM crm_cases WHERE id=?", (case_id,)).fetchone()
        if not case:
            raise CRMError(404, "Case not found.")
        if case["status"] == "pending" and selection is None:
            _save_requirements(conn, case_id, pincode, skill)
            _audit(conn, case_id, "eligibility_configured", reason, owner=owner)
            return {"state": "awaiting_case_acceptance", "assignment": None}
        if case["status"] != "accepted":
            raise CRMError(409, "Accept the case before assigning a technician.")
        if conn.execute(
            "SELECT 1 FROM technician_assignments WHERE case_id=? AND status='completed'", (case_id,),
        ).fetchone():
            raise CRMError(409, "This case already has a completed visit.")
        now = int(time.time())
        _expire_offers(conn, now)
        return _dispatch(conn, case_id, pincode, skill, owner, now, selection, reason)


def _save_requirements(conn, case_id, pincode, skill):
    conn.execute(
        """INSERT INTO dispatch_requirements VALUES (?,?,?) ON CONFLICT(case_id)
           DO UPDATE SET pincode=excluded.pincode,skill=excluded.skill""", (case_id, pincode, skill),
    )


def assign_on_acceptance(conn, case_id, owner):
    requirements = conn.execute("SELECT * FROM dispatch_requirements WHERE case_id=?", (case_id,)).fetchone()
    if not requirements:
        _notify(conn, case_id, "eligibility_required")
        return
    now = int(time.time())
    _expire_offers(conn, now)
    _dispatch(conn, case_id, requirements["pincode"], requirements["skill"], owner, now, None, "Automatic assignment on case acceptance.")


def save_preference(case_id, preference, owner):
    with connection() as conn:
        begin_write(conn)
        case = conn.execute("SELECT status FROM crm_cases WHERE id=?", (case_id,)).fetchone()
        if not case:
            raise CRMError(404, "Case not found.")
        if case["status"] not in ("pending", "accepted"):
            raise CRMError(409, "This case is closed for scheduling.")
        conn.execute(
            "INSERT INTO dispatch_preferences VALUES (?,?) ON CONFLICT(case_id) DO UPDATE SET preference=excluded.preference",
            (case_id, preference),
        )
        _audit(conn, case_id, "appointment_preference_saved", "Customer preference recorded.", owner=owner)


def release(assignment_id, version, reason, owner):
    with connection() as conn:
        begin_write(conn)
        job = _assignment(conn, assignment_id)
        if job["version"] != version or job["status"] not in WORKFLOW:
            raise CRMError(409, "The assignment version or release is invalid.")
        return _set_status(conn, job, "released", reason, owner=owner)


def change_job(assignment_id, version, status, reason, technician):
    now = int(time.time())
    result = None
    with connection() as conn:
        begin_write(conn)
        job = _assignment(conn, assignment_id, technician)
        if job["status"] == "offered" and job["offered_until"] <= now:
            _set_status(conn, job, "expired", "Offer acceptance window expired.")
            requirements = conn.execute("SELECT * FROM dispatch_requirements WHERE case_id=?", (job["case_id"],)).fetchone()
            _dispatch(conn, job["case_id"], requirements["pincode"], requirements["skill"], None, now, None, "Reassignment after timeout.")
        else:
            if job["version"] != version or status not in WORKFLOW.get(job["status"], set()):
                raise CRMError(409, "The assignment version or requested transition is invalid.")
            if status == "accepted" and not _working(conn, technician, now, now):
                raise CRMError(409, "This offer cannot be accepted while unavailable.")
            if status == "en_route" and not conn.execute(
                "SELECT 1 FROM dispatch_appointments WHERE assignment_id=? AND status='confirmed'", (assignment_id,),
            ).fetchone():
                raise CRMError(409, "Confirm an appointment before starting travel.")
            result = _set_status(conn, job, status, reason, technician=technician)
            if status == "declined":
                requirements = conn.execute("SELECT * FROM dispatch_requirements WHERE case_id=?", (job["case_id"],)).fetchone()
                _dispatch(conn, job["case_id"], requirements["pincode"], requirements["skill"], None, now, None, "Reassignment after decline.")
    if result is None:
        raise CRMError(409, "This offer has expired.")
    return result


def process_timeouts():
    with connection() as conn:
        begin_write(conn)
        now = int(time.time())
        _expire_offers(conn, now)
        conn.execute(
            """INSERT INTO dispatch_notifications(technician_id,case_id,kind,created_at,dedupe_key)
               SELECT a.technician_id,a.case_id,'appointment_reminder',?,
                      'reminder:'||p.id||':'||p.version
               FROM dispatch_appointments p JOIN technician_assignments a ON a.id=p.assignment_id
               WHERE p.status='confirmed' AND p.starts_at>? AND p.starts_at<=?
               ON CONFLICT(dedupe_key) DO NOTHING""", (now_iso(), now, now + 3600),
        )


def _booking_conflict(conn, technician_id, start, end, except_id=None):
    return conn.execute(
        """SELECT 1 FROM dispatch_appointments p JOIN technician_assignments a ON a.id=p.assignment_id
           WHERE a.technician_id=? AND p.status='confirmed' AND p.starts_at<? AND p.ends_at>?
             AND p.id IS DISTINCT FROM ? LIMIT 1""", (technician_id, end, start, except_id),
    ).fetchone() is not None


def save_appointment(assignment_id, start, end, reason, owner, version=None):
    now = int(time.time())
    if not now < start < end <= now + BOOKING_HORIZON:
        raise CRMError(422, "Provide a future appointment within 180 days with an end after its start.")
    with connection() as conn:
        begin_write(conn)
        job = _assignment(conn, assignment_id)
        if job["status"] != "accepted":
            raise CRMError(409, "Book an appointment for an accepted assignment.")
        current = conn.execute(
            "SELECT * FROM dispatch_appointments WHERE assignment_id=? AND status='confirmed'", (assignment_id,),
        ).fetchone()
        if (current and version != current["version"]) or (not current and version is not None):
            raise CRMError(409, "The appointment version is stale.")
        appointment_id = current["id"] if current else None
        if not _working(conn, job["technician_id"], start, end) or _booking_conflict(
            conn, job["technician_id"], start, end, appointment_id,
        ):
            raise CRMError(409, "That interval is unavailable.")
        if current:
            conn.execute("UPDATE dispatch_appointments SET starts_at=?,ends_at=?,version=version+1 WHERE id=?",
                         (start, end, appointment_id))
        else:
            appointment_id = conn.execute(
                "INSERT INTO dispatch_appointments(assignment_id,starts_at,ends_at,status) VALUES (?,?,?,'confirmed') RETURNING id",
                (assignment_id, start, end),
            ).fetchone()[0]
        event = "appointment_rescheduled" if current else "appointment_confirmed"
        _audit(conn, job["case_id"], event, reason, owner=owner, technician=job["technician_id"])
        _notify(conn, job["case_id"], event, job["technician_id"])
        return dict(conn.execute("SELECT * FROM dispatch_appointments WHERE id=?", (appointment_id,)).fetchone())


def available_slots(assignment_id, day, duration):
    now = int(time.time())
    if not datetime.fromtimestamp(now, KOLKATA).date() <= day <= datetime.fromtimestamp(now + BOOKING_HORIZON, KOLKATA).date():
        raise CRMError(422, "Choose a business date within the booking horizon.")
    midnight = int(datetime.combine(day, day_time.min, KOLKATA).timestamp())
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        job = _assignment(conn, assignment_id)
        if job["status"] != "accepted":
            raise CRMError(409, "Find slots for an accepted assignment.")
        hours = conn.execute(
            "SELECT start_minute,end_minute FROM technician_hours WHERE technician_id=? AND weekday=? ORDER BY start_minute",
            (job["technician_id"], day.weekday()),
        ).fetchall()
        slots = []
        for window in hours:
            for minute in range(window["start_minute"], window["end_minute"] - duration + 1, duration):
                start = midnight + minute * 60
                end = start + duration * 60
                if now < start < end <= now + BOOKING_HORIZON and _working(
                    conn, job["technician_id"], start, end,
                ) and not _booking_conflict(conn, job["technician_id"], start, end):
                    slots.append({"starts_at": start, "ends_at": end})
        return slots


def close_appointment(assignment_id, version, status, reason, owner):
    with connection() as conn:
        begin_write(conn)
        job = _assignment(conn, assignment_id)
        booking = conn.execute("SELECT * FROM dispatch_appointments WHERE assignment_id=? AND status='confirmed'",
                               (assignment_id,)).fetchone()
        if not booking or booking["version"] != version or job["status"] not in ("accepted", "en_route"):
            raise CRMError(409, "The appointment version or closure is invalid.")
        if status == "no_show" and booking["ends_at"] > int(time.time()):
            raise CRMError(409, "Record a no-show after the booked interval.")
        conn.execute("UPDATE dispatch_appointments SET status=?,version=version+1 WHERE id=?", (status, booking["id"]))
        if job["status"] == "en_route":
            # Restore the accepted job so dispatch can book a replacement slot.
            _set_status(conn, job, "accepted", "Previous booking closed before work began.", owner=owner)
        _audit(conn, job["case_id"], f"appointment_{status}", reason, owner=owner, technician=job["technician_id"])
        _notify(conn, job["case_id"], f"appointment_{status}", job["technician_id"])


def job_list(technician):
    with connection(read_only=True) as conn:
        return [dict(row) for row in conn.execute(
            f"""SELECT a.id,a.status,a.version,a.offered_until,c.reference,r.pincode,r.skill
                FROM technician_assignments a JOIN crm_cases c ON c.id=a.case_id
                JOIN dispatch_requirements r ON r.case_id=a.case_id
                WHERE a.technician_id=? AND a.status IN ({ACTIVE})
                  AND (a.status<>'offered' OR a.offered_until>?) ORDER BY a.id""", (technician, int(time.time())),
        )]


def job_detail(assignment_id, technician):
    with connection(read_only=True) as conn:
        conn.execute("BEGIN")
        job = _assignment(conn, assignment_id, technician)
        if job["status"] not in WORKFLOW or (job["status"] == "offered" and job["offered_until"] <= int(time.time())):
            raise CRMError(404, "Assignment not found.")
        case = conn.execute(
            """SELECT c.reference,c.description,c.priority,r.pincode,r.skill,p.product_name,p.model_number,p.serial_number
               FROM crm_cases c JOIN dispatch_requirements r ON r.case_id=c.id
               LEFT JOIN products p ON p.id=c.product_id WHERE c.id=?""", (job["case_id"],),
        ).fetchone()
        result = {key: job[key] for key in ("id", "status", "version", "offered_until")}
        result.update({key: case[key] for key in ("reference", "priority", "pincode", "skill")})
        if job["status"] != "offered":
            result.update({key: case[key] for key in ("description", "product_name", "model_number", "serial_number")})
            booking = conn.execute(
                "SELECT starts_at,ends_at FROM dispatch_appointments WHERE assignment_id=? AND status='confirmed'", (assignment_id,),
            ).fetchone()
            result["appointment"] = dict(booking) if booking else None
            result["notes"] = [dict(row) for row in conn.execute(
                "SELECT kind,body,created_at FROM technician_job_notes WHERE assignment_id=? ORDER BY id", (assignment_id,),
            )]
            if booking:
                contact = conn.execute(
                    """SELECT u.name,u.address,
                       CASE WHEN count(v.id)=1 THEN min(v.mobile) END AS verified_mobile
                       FROM crm_cases c JOIN customers u ON u.id=c.customer_id
                       LEFT JOIN crm_contacts v ON v.customer_id=u.id WHERE c.id=? GROUP BY u.id""", (job["case_id"],),
                ).fetchone()
                result["visit_contact"] = dict(contact)
        return result


def add_note(assignment_id, kind, body, technician):
    with connection() as conn:
        begin_write(conn)
        job = _assignment(conn, assignment_id, technician)
        if job["status"] not in {"accepted", "en_route", "in_progress", "awaiting_parts"}:
            raise CRMError(409, "Accept the job before adding notes.")
        note_id = conn.execute("INSERT INTO technician_job_notes(assignment_id,kind,body,created_at) VALUES (?,?,?,?) RETURNING id",
                               (assignment_id, kind, body, now_iso())).fetchone()[0]
        _audit(conn, job["case_id"], "job_note_added", f"Technician added a {kind} entry.", technician=technician)
        if kind == "escalation":
            _notify(conn, job["case_id"], "technician_escalation")
        return note_id


def case_dispatch(case_id):
    with connection(read_only=True) as conn:
        if not conn.execute("SELECT 1 FROM crm_cases WHERE id=?", (case_id,)).fetchone():
            raise CRMError(404, "Case not found.")
        assignments = [dict(row) for row in conn.execute("SELECT * FROM technician_assignments WHERE case_id=? ORDER BY id", (case_id,))]
        appointments = [dict(row) for row in conn.execute(
            """SELECT p.* FROM dispatch_appointments p JOIN technician_assignments a ON a.id=p.assignment_id
               WHERE a.case_id=? ORDER BY p.id""", (case_id,),
        )]
        history = conn.execute("SELECT * FROM dispatch_audit WHERE case_id=? ORDER BY id", (case_id,))
        notes = conn.execute(
            """SELECT n.* FROM technician_job_notes n JOIN technician_assignments a ON a.id=n.assignment_id
               WHERE a.case_id=? ORDER BY n.id""", (case_id,),
        )
        preference = conn.execute("SELECT preference FROM dispatch_preferences WHERE case_id=?", (case_id,)).fetchone()
        return {"assignments": assignments, "appointments": appointments, "preference": preference[0] if preference else None,
                "history": [dict(row) for row in history], "notes": [dict(row) for row in notes]}


def notifications(technician):
    with connection(read_only=True) as conn:
        return [dict(row) for row in conn.execute(
            """SELECT id,case_id,kind,created_at FROM dispatch_notifications
               WHERE technician_id IS NOT DISTINCT FROM ? AND acknowledged_at IS NULL ORDER BY id LIMIT 100""", (technician,),
        )]


def acknowledge(notification_id, technician):
    with connection() as conn:
        updated = conn.execute(
            "UPDATE dispatch_notifications SET acknowledged_at=coalesce(acknowledged_at,?) WHERE id=? AND technician_id IS NOT DISTINCT FROM ?",
            (now_iso(), notification_id, technician),
        )
        if not updated.rowcount:
            raise CRMError(404, "Notification not found.")
