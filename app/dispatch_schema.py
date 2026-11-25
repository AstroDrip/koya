"""Additive technician and scheduling tables."""

DISPATCH_SCHEMA = """
CREATE TABLE technicians (
    id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)), capacity INTEGER NOT NULL CHECK(capacity BETWEEN 1 AND 50)
);
CREATE TABLE technician_sessions (
    verifier TEXT PRIMARY KEY, technician_id INTEGER NOT NULL REFERENCES technicians(id), expires_at INTEGER NOT NULL
);
CREATE INDEX idx_technician_sessions_expiry ON technician_sessions(expires_at);
CREATE TABLE technician_coverage (
    technician_id INTEGER NOT NULL REFERENCES technicians(id), pincode TEXT NOT NULL,
    PRIMARY KEY(technician_id,pincode)
);
CREATE INDEX idx_technician_coverage_pincode ON technician_coverage(pincode,technician_id);
CREATE TABLE technician_skills (
    technician_id INTEGER NOT NULL REFERENCES technicians(id), skill TEXT NOT NULL,
    PRIMARY KEY(technician_id,skill)
);
CREATE TABLE technician_hours (
    technician_id INTEGER NOT NULL REFERENCES technicians(id), weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
    start_minute INTEGER NOT NULL, end_minute INTEGER NOT NULL CHECK(start_minute < end_minute),
    PRIMARY KEY(technician_id,weekday,start_minute)
);
CREATE TABLE technician_leave (
    id INTEGER PRIMARY KEY, technician_id INTEGER NOT NULL REFERENCES technicians(id),
    starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL CHECK(ends_at > starts_at), reason TEXT NOT NULL
);
CREATE INDEX idx_technician_leave_time ON technician_leave(technician_id,starts_at,ends_at);
CREATE TABLE dispatch_requirements (
    case_id INTEGER PRIMARY KEY REFERENCES crm_cases(id), pincode TEXT NOT NULL, skill TEXT NOT NULL
);
CREATE TABLE dispatch_preferences (
    case_id INTEGER PRIMARY KEY REFERENCES crm_cases(id), preference TEXT NOT NULL
);
CREATE TABLE technician_assignments (
    id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES crm_cases(id),
    technician_id INTEGER NOT NULL REFERENCES technicians(id), status TEXT NOT NULL
        CHECK(status IN ('offered','accepted','en_route','in_progress','awaiting_parts','completed','declined','released','expired')),
    version INTEGER NOT NULL DEFAULT 1, offered_until INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_assignment_active_case ON technician_assignments(case_id)
    WHERE status IN ('offered','accepted','en_route','in_progress','awaiting_parts');
CREATE INDEX idx_assignment_technician ON technician_assignments(technician_id,status,id);
CREATE TABLE dispatch_appointments (
    id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL REFERENCES technician_assignments(id),
    starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL CHECK(ends_at > starts_at),
    status TEXT NOT NULL CHECK(status IN ('confirmed','cancelled','completed','no_show')),
    version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX idx_confirmed_appointment ON dispatch_appointments(assignment_id) WHERE status='confirmed';
CREATE TABLE technician_job_notes (
    id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL REFERENCES technician_assignments(id),
    kind TEXT NOT NULL CHECK(kind IN ('note','inspection','parts','escalation')),
    body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX idx_job_note_assignment ON technician_job_notes(assignment_id,id);
CREATE TABLE dispatch_audit (
    id INTEGER PRIMARY KEY, case_id INTEGER REFERENCES crm_cases(id),
    owner_id INTEGER REFERENCES staff_users(id), technician_id INTEGER REFERENCES technicians(id),
    event TEXT NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE INDEX idx_dispatch_audit_case ON dispatch_audit(case_id,id);
CREATE TABLE dispatch_notifications (
    id INTEGER PRIMARY KEY, technician_id INTEGER REFERENCES technicians(id),
    case_id INTEGER NOT NULL REFERENCES crm_cases(id), kind TEXT NOT NULL,
    created_at TEXT NOT NULL, acknowledged_at TEXT, dedupe_key TEXT UNIQUE
);
CREATE INDEX idx_dispatch_notifications_recipient ON dispatch_notifications(technician_id,acknowledged_at,id);
"""
