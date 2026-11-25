"""Call-centre persistence. Recordings remain at the provider."""

VOICE_SCHEMA = """
CREATE TABLE voice_calls (
    id INTEGER PRIMARY KEY, app_id TEXT NOT NULL, deployment_id TEXT, app_version INTEGER,
    direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
    interaction_id TEXT, attempt_id TEXT, caller_number TEXT, agent_number TEXT,
    state TEXT NOT NULL CHECK(state IN ('started','ended','completed','submitted','connected','no_answer','busy','failed')),
    started_at INTEGER, ended_at INTEGER, observed_at INTEGER NOT NULL, source TEXT NOT NULL,
    duration_seconds REAL CHECK(duration_seconds>=0), transcript_reported INTEGER NOT NULL DEFAULT 0,
    reported_outcome TEXT, failure_reason TEXT,
    case_id INTEGER REFERENCES crm_cases(id), customer_id INTEGER REFERENCES customers(id),
    media_allowed INTEGER NOT NULL DEFAULT 0 CHECK(media_allowed IN (0,1)), media_until INTEGER,
    UNIQUE(app_id,interaction_id), UNIQUE(app_id,attempt_id)
);
CREATE INDEX idx_voice_recent ON voice_calls(observed_at,id);
CREATE INDEX idx_voice_case ON voice_calls(case_id,id);
CREATE TABLE voice_event_inbox (
    app_id TEXT NOT NULL, kind TEXT NOT NULL, event_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL, received_at INTEGER NOT NULL,
    PRIMARY KEY(app_id,kind,event_key)
);
CREATE TABLE voice_audit (
    id INTEGER PRIMARY KEY, call_id INTEGER REFERENCES voice_calls(id), owner_id INTEGER REFERENCES staff_users(id),
    contact_id INTEGER REFERENCES crm_contacts(id), followup_id INTEGER REFERENCES voice_followups(id),
    action TEXT NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE INDEX idx_voice_audit_call ON voice_audit(call_id,id);
CREATE TABLE voice_recording_metadata (
    call_id INTEGER PRIMARY KEY REFERENCES voice_calls(id), content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE TABLE voice_permissions (
    contact_id INTEGER PRIMARY KEY REFERENCES crm_contacts(id),
    permitted INTEGER NOT NULL CHECK(permitted IN (0,1)), at TEXT NOT NULL,
    evidence TEXT NOT NULL, owner_id INTEGER NOT NULL REFERENCES staff_users(id), version INTEGER NOT NULL
);
CREATE TABLE voice_followups (
    id INTEGER PRIMARY KEY, operation_key TEXT NOT NULL UNIQUE, case_id INTEGER NOT NULL REFERENCES crm_cases(id),
    contact_id INTEGER NOT NULL REFERENCES crm_contacts(id), appointment_id INTEGER NOT NULL REFERENCES dispatch_appointments(id),
    appointment_version INTEGER NOT NULL, permission_version INTEGER NOT NULL, requested_by INTEGER NOT NULL REFERENCES staff_users(id),
    state TEXT NOT NULL CHECK(state IN ('queued','dispatching','submitted','connected','no_answer','busy','failed','uncertain','cancelled')),
    call_id INTEGER UNIQUE REFERENCES voice_calls(id), retry_of INTEGER UNIQUE REFERENCES voice_followups(id),
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, informed_at INTEGER, disposition TEXT
);
CREATE UNIQUE INDEX idx_voice_followup_open ON voice_followups(case_id,contact_id)
    WHERE state IN ('queued','dispatching','submitted','uncertain');
CREATE TABLE voice_escalations (
    id INTEGER PRIMARY KEY, call_id INTEGER NOT NULL REFERENCES voice_calls(id), category TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('requested','resolved')), requested_at TEXT NOT NULL,
    resolved_at TEXT, resolved_by INTEGER REFERENCES staff_users(id), resolution TEXT,
    UNIQUE(call_id,category)
);
"""
