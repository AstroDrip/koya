"""Additive finance records. Monetary columns hold INR paise."""

FINANCE_SCHEMA = """
CREATE TABLE finance_accounts (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, active INTEGER NOT NULL CHECK(active IN (0,1))
);
CREATE TABLE finance_sessions (
    verifier TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES finance_accounts(id), expires_at INTEGER NOT NULL
);
CREATE INDEX idx_finance_sessions_expiry ON finance_sessions(expires_at);
CREATE TABLE fin_issuer_profiles (
    id INTEGER PRIMARY KEY, legal_name TEXT NOT NULL, address TEXT NOT NULL,
    tax_registration TEXT, tax_evidence TEXT, configured_at INTEGER NOT NULL, actor TEXT NOT NULL
);
CREATE TABLE fin_fee_policies (
    id INTEGER PRIMARY KEY, minimum_paise INTEGER NOT NULL CHECK(minimum_paise>=0),
    evidence TEXT NOT NULL, configured_at INTEGER NOT NULL, actor TEXT NOT NULL
);
CREATE TABLE fin_price_versions (
    id INTEGER PRIMARY KEY, code TEXT NOT NULL, revision INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('inspection','labor','part')), label TEXT NOT NULL,
    product_id INTEGER REFERENCES products(id), unit_paise INTEGER NOT NULL CHECK(unit_paise>=0),
    tax_bps INTEGER NOT NULL CHECK(tax_bps BETWEEN 0 AND 10000), tax_label TEXT, classification TEXT,
    issuer_id INTEGER REFERENCES fin_issuer_profiles(id), active INTEGER NOT NULL CHECK(active IN (0,1)),
    evidence TEXT NOT NULL, approved_at INTEGER NOT NULL, actor TEXT NOT NULL, UNIQUE(code,revision)
);
CREATE TABLE fin_estimates (
    id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES crm_cases(id),
    product_id INTEGER REFERENCES products(id), revision INTEGER NOT NULL, previous_id INTEGER REFERENCES fin_estimates(id),
    technician_id INTEGER REFERENCES technicians(id), fee_policy_id INTEGER REFERENCES fin_fee_policies(id),
    operation_key TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL, notes TEXT, exclusions TEXT,
    expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL, actor TEXT NOT NULL,
    net_paise INTEGER NOT NULL, tax_paise INTEGER NOT NULL, total_paise INTEGER NOT NULL,
    UNIQUE(case_id,revision)
);
CREATE TABLE fin_estimate_lines (
    estimate_id INTEGER NOT NULL REFERENCES fin_estimates(id), line_no INTEGER NOT NULL,
    price_version_id INTEGER NOT NULL REFERENCES fin_price_versions(id),
    quantity INTEGER NOT NULL CHECK(quantity>0), gross_paise INTEGER NOT NULL, discount_paise INTEGER NOT NULL,
    net_paise INTEGER NOT NULL, tax_paise INTEGER NOT NULL, total_paise INTEGER NOT NULL,
    PRIMARY KEY(estimate_id,line_no)
);
CREATE TABLE fin_estimate_events (
    id INTEGER PRIMARY KEY, estimate_id INTEGER NOT NULL REFERENCES fin_estimates(id),
    status TEXT NOT NULL CHECK(status IN ('submitted','approved','accepted','rejected','superseded')),
    actor TEXT NOT NULL, evidence TEXT NOT NULL, occurred_at INTEGER NOT NULL,
    UNIQUE(estimate_id,status)
);
CREATE INDEX idx_fin_estimate_events ON fin_estimate_events(estimate_id,id);
CREATE TABLE fin_sequences (
    series TEXT NOT NULL, fiscal_year INTEGER NOT NULL, last_number INTEGER NOT NULL,
    PRIMARY KEY(series,fiscal_year)
);
CREATE TABLE fin_invoices (
    id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE,
    estimate_id INTEGER NOT NULL UNIQUE REFERENCES fin_estimates(id), case_id INTEGER NOT NULL REFERENCES crm_cases(id),
    technician_id INTEGER REFERENCES technicians(id), issuer_id INTEGER NOT NULL REFERENCES fin_issuer_profiles(id),
    operation_key TEXT NOT NULL UNIQUE, snapshot_json TEXT NOT NULL, issued_at INTEGER NOT NULL, actor TEXT NOT NULL,
    gross_paise INTEGER NOT NULL, discount_paise INTEGER NOT NULL,
    net_paise INTEGER NOT NULL, tax_paise INTEGER NOT NULL, total_paise INTEGER NOT NULL
);
CREATE INDEX idx_fin_invoices_case ON fin_invoices(case_id,id);
CREATE INDEX idx_fin_invoices_time ON fin_invoices(issued_at,id);
CREATE TABLE fin_voids (
    invoice_id INTEGER PRIMARY KEY REFERENCES fin_invoices(id), evidence TEXT NOT NULL, actor TEXT NOT NULL, occurred_at INTEGER NOT NULL
);
CREATE TABLE fin_credits (
    id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE, invoice_id INTEGER NOT NULL REFERENCES fin_invoices(id),
    operation_key TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
    net_paise INTEGER NOT NULL, tax_paise INTEGER NOT NULL, total_paise INTEGER NOT NULL,
    evidence TEXT NOT NULL, actor TEXT NOT NULL, issued_at INTEGER NOT NULL
);
CREATE INDEX idx_fin_credits_invoice ON fin_credits(invoice_id,id);
CREATE INDEX idx_fin_credits_time ON fin_credits(issued_at,id);
CREATE TABLE fin_credit_lines (
    credit_id INTEGER NOT NULL REFERENCES fin_credits(id), line_no INTEGER NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity>0), net_paise INTEGER NOT NULL, tax_paise INTEGER NOT NULL,
    PRIMARY KEY(credit_id,line_no)
);
CREATE TABLE fin_cash_events (
    id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE, invoice_id INTEGER NOT NULL REFERENCES fin_invoices(id),
    kind TEXT NOT NULL CHECK(kind IN ('payment','refund')), amount_paise INTEGER NOT NULL CHECK(amount_paise>0),
    method TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL,
    payment_id INTEGER REFERENCES fin_cash_events(id), credit_id INTEGER REFERENCES fin_credits(id),
    actor TEXT NOT NULL, evidence TEXT NOT NULL, occurred_at INTEGER NOT NULL, recorded_at INTEGER NOT NULL,
    UNIQUE(source,external_id),
    CHECK((kind='payment' AND payment_id IS NULL AND credit_id IS NULL)
       OR (kind='refund' AND payment_id IS NOT NULL AND credit_id IS NOT NULL))
);
CREATE INDEX idx_fin_cash_invoice ON fin_cash_events(invoice_id,id);
CREATE INDEX idx_fin_cash_time ON fin_cash_events(occurred_at,id);
CREATE INDEX idx_fin_cash_payment ON fin_cash_events(payment_id);
CREATE INDEX idx_fin_cash_credit ON fin_cash_events(credit_id);
"""

IMMUTABLE_FINANCE_TABLES = (
    "fin_issuer_profiles", "fin_fee_policies", "fin_price_versions", "fin_estimates", "fin_estimate_lines",
    "fin_estimate_events", "fin_invoices", "fin_voids", "fin_credits", "fin_credit_lines", "fin_cash_events",
)
