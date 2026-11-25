"""WhatsApp inbox, access scopes, and transactional outbox."""

WHATSAPP_SCHEMA = """
CREATE TABLE crm_channel_claims (
    case_id INTEGER PRIMARY KEY REFERENCES crm_cases(id),
    channel TEXT NOT NULL CHECK(channel IN ('voice','whatsapp')), owner_key TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE wa_peers (
    id INTEGER PRIMARY KEY, phone_id TEXT NOT NULL, sender TEXT NOT NULL,
    last_inbound INTEGER, preference TEXT NOT NULL DEFAULT 'unknown'
        CHECK(preference IN ('unknown','opted_in','opted_out')),
    preference_at INTEGER, requested_reference TEXT, requested_at INTEGER, scope_revision INTEGER NOT NULL DEFAULT 0,
    case_id INTEGER REFERENCES crm_cases(id), contact_id INTEGER REFERENCES crm_contacts(id),
    token_id INTEGER REFERENCES crm_case_tokens(id), verified_at INTEGER, verified_until INTEGER,
    UNIQUE(phone_id,sender)
);
CREATE TABLE wa_messages (
    id INTEGER PRIMARY KEY, phone_id TEXT NOT NULL, peer_id INTEGER NOT NULL REFERENCES wa_peers(id),
    provider_id TEXT NOT NULL, sent_at INTEGER NOT NULL, kind TEXT NOT NULL,
    body TEXT, conversation_id INTEGER REFERENCES crm_conversations(id),
    UNIQUE(phone_id,provider_id)
);
CREATE INDEX idx_wa_message_conversation ON wa_messages(conversation_id,id);
CREATE TABLE wa_verifications (
    challenge_id TEXT PRIMARY KEY REFERENCES crm_verification_challenges(id),
    peer_id INTEGER NOT NULL REFERENCES wa_peers(id), scope_revision INTEGER NOT NULL
);
CREATE TABLE wa_consent_history (
    id INTEGER PRIMARY KEY, peer_id INTEGER NOT NULL REFERENCES wa_peers(id),
    preference TEXT NOT NULL, occurred_at INTEGER NOT NULL, source TEXT NOT NULL, evidence TEXT NOT NULL,
    owner_id INTEGER REFERENCES staff_users(id)
);
CREATE TABLE wa_handoffs (
    id INTEGER PRIMARY KEY, peer_id INTEGER NOT NULL REFERENCES wa_peers(id),
    conversation_id INTEGER REFERENCES crm_conversations(id),
    status TEXT NOT NULL CHECK(status IN ('open','closed')), opened_at INTEGER NOT NULL,
    closed_at INTEGER, closed_by INTEGER REFERENCES staff_users(id), resolution TEXT
);
CREATE UNIQUE INDEX idx_wa_open_handoff ON wa_handoffs(peer_id,ifnull(conversation_id,0)) WHERE status='open';
CREATE TABLE wa_outbox (
    id INTEGER PRIMARY KEY, peer_id INTEGER NOT NULL REFERENCES wa_peers(id),
    conversation_id INTEGER REFERENCES crm_conversations(id), operation_key TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL CHECK(purpose IN ('system','case_status','owner_text','template')),
    body TEXT, template_name TEXT, template_language TEXT, scope_revision INTEGER,
    state TEXT NOT NULL CHECK(state IN ('queued','sending','submitted','failed','uncertain','cancelled')),
    provider_id TEXT UNIQUE, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, outcome_note TEXT, sent_body TEXT,
    owner_id INTEGER REFERENCES staff_users(id)
);
CREATE INDEX idx_wa_outbox_ready ON wa_outbox(state,id);
CREATE TABLE wa_send_rate (
    phone_id TEXT NOT NULL, bucket INTEGER NOT NULL, scope TEXT NOT NULL, submissions INTEGER NOT NULL,
    PRIMARY KEY(phone_id,bucket,scope)
);
CREATE TABLE wa_receipts (
    phone_id TEXT NOT NULL, provider_id TEXT NOT NULL, recipient TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('sent','delivered','read','failed')), occurred_at INTEGER NOT NULL,
    PRIMARY KEY(phone_id,provider_id,recipient,status)
);
"""
