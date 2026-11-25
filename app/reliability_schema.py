"""Durable operational change notifications and retryable worker tasks."""

RELIABILITY_SCHEMA = """
CREATE TABLE ops_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, resource_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE ops_retention (
    marker INTEGER PRIMARY KEY CHECK(marker=1), pruned_through INTEGER NOT NULL
);
CREATE TABLE ops_jobs (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, resource_id INTEGER NOT NULL,
    operation_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL CHECK(state IN ('queued','running','done','dead')),
    due_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, lease_until INTEGER, lease_id TEXT,
    created_at INTEGER NOT NULL, finished_at INTEGER, outcome TEXT
);
CREATE INDEX idx_ops_job_due ON ops_jobs(state,due_at,id);
CREATE INDEX idx_ops_job_lease ON ops_jobs(state,lease_until,id);
"""

RECORDING_STORAGE_COLUMNS = (
    "ALTER TABLE voice_recording_metadata ADD COLUMN storage_key TEXT",
    "ALTER TABLE voice_recording_metadata ADD COLUMN retention_until INTEGER",
)
