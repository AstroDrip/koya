"""Additive SQLite schema for the CRM foundation."""

CRM_SCHEMA = """
CREATE TABLE crm_customer_creations (
    operation_key TEXT PRIMARY KEY, customer_id INTEGER NOT NULL UNIQUE REFERENCES customers(id)
);
CREATE TABLE crm_contacts (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
    mobile TEXT NOT NULL UNIQUE, verified_at TEXT NOT NULL
);
CREATE INDEX idx_crm_contact_customer ON crm_contacts(customer_id);
CREATE INDEX idx_crm_product_customer ON products(customer_id);
CREATE TABLE crm_case_sequences (
    business_date TEXT PRIMARY KEY, last_number INTEGER NOT NULL CHECK(last_number BETWEEN 1 AND 999)
);
CREATE TABLE crm_cases (
    id INTEGER PRIMARY KEY, reference TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id), product_id INTEGER REFERENCES products(id),
    channel TEXT NOT NULL CHECK(channel IN ('voice','whatsapp','staff')),
    description TEXT NOT NULL, confirmation_text TEXT NOT NULL,
    category TEXT, priority TEXT NOT NULL CHECK(priority IN ('normal','urgent')),
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','cancelled','rejected')),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    operation_key TEXT NOT NULL UNIQUE, operation_hash TEXT NOT NULL
);
CREATE INDEX idx_crm_cases_customer ON crm_cases(customer_id,id);
CREATE INDEX idx_crm_cases_status ON crm_cases(status,id);
CREATE TABLE crm_case_history (
    id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES crm_cases(id),
    version INTEGER NOT NULL, previous_status TEXT, status TEXT NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES staff_users(id), source TEXT NOT NULL,
    reason TEXT NOT NULL, occurred_at TEXT NOT NULL, UNIQUE(case_id,version)
);
CREATE TABLE crm_conversations (
    id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL UNIQUE REFERENCES crm_cases(id)
);
CREATE TABLE crm_interactions (
    id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES crm_conversations(id),
    channel TEXT NOT NULL CHECK(channel IN ('voice','whatsapp','staff')),
    provider TEXT NOT NULL, provider_account TEXT NOT NULL, external_id TEXT NOT NULL,
    summary TEXT NOT NULL, facts_json TEXT NOT NULL, created_at TEXT NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES staff_users(id),
    UNIQUE(channel,provider,provider_account,external_id)
);
CREATE INDEX idx_crm_interactions_conversation ON crm_interactions(conversation_id,id);
CREATE TABLE crm_case_tokens (
    id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES crm_cases(id), verifier TEXT NOT NULL UNIQUE,
    expires_at INTEGER NOT NULL, revoked_at INTEGER
);
CREATE UNIQUE INDEX idx_crm_active_token ON crm_case_tokens(case_id) WHERE revoked_at IS NULL;
CREATE TABLE crm_verification_challenges (
    id TEXT PRIMARY KEY, purpose TEXT NOT NULL CHECK(purpose IN ('enroll','resume')),
    customer_id INTEGER NOT NULL REFERENCES customers(id), mobile TEXT NOT NULL,
    token_id INTEGER REFERENCES crm_case_tokens(id), verifier TEXT NOT NULL,
    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0, used_at INTEGER,
    CHECK((purpose = 'resume' AND token_id IS NOT NULL) OR (purpose = 'enroll' AND token_id IS NULL))
);
CREATE INDEX idx_crm_challenge_mobile ON crm_verification_challenges(mobile,created_at);
CREATE TABLE crm_verification_limits (
    scope TEXT NOT NULL, bucket INTEGER NOT NULL, attempts INTEGER NOT NULL,
    PRIMARY KEY(scope,bucket)
);
CREATE TABLE crm_identity_audit (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
    event TEXT NOT NULL, actor_id INTEGER REFERENCES staff_users(id), created_at TEXT NOT NULL
);
"""
