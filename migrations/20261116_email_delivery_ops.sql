-- Additive schema contract for the Python migration in
-- app.services.email_delivery_ops.apply_email_delivery_ops_migration.
-- The Python runner creates only these four tables and indexes, then installs
-- the append-only trigger in the same caller-owned transaction. No existing
-- table or row is dropped, recreated, or rewritten.

CREATE TABLE IF NOT EXISTS email_delivery_attempt (
    id SERIAL NOT NULL PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES email_message(id) ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL,
    attempt_key VARCHAR(36) NOT NULL UNIQUE,
    provider VARCHAR(24) NOT NULL,
    status VARCHAR(24) NOT NULL,
    started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    completed_at TIMESTAMP WITHOUT TIME ZONE,
    provider_message_id VARCHAR(255),
    error_category VARCHAR(48),
    recovery_of_attempt_id INTEGER REFERENCES email_delivery_attempt(id) ON DELETE RESTRICT,
    CONSTRAINT uq_email_attempt_message_number UNIQUE (message_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS ix_email_attempt_message_started
    ON email_delivery_attempt (message_id, started_at);

CREATE TABLE IF NOT EXISTS email_delivery_investigation (
    id SERIAL NOT NULL PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES email_message(id) ON DELETE RESTRICT,
    original_attempt_id INTEGER REFERENCES email_delivery_attempt(id) ON DELETE RESTRICT,
    provider VARCHAR(24) NOT NULL,
    provider_message_id VARCHAR(255),
    reason VARCHAR(255) NOT NULL,
    status VARCHAR(40) NOT NULL,
    evidence_summary TEXT,
    evidence_source VARCHAR(255),
    investigator_admin_id INTEGER REFERENCES admin_user(id) ON DELETE RESTRICT,
    started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    resolved_at TIMESTAMP WITHOUT TIME ZONE,
    resolution_outcome VARCHAR(40),
    resolution_explanation TEXT,
    recovered_message_id INTEGER UNIQUE REFERENCES email_message(id) ON DELETE RESTRICT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    CONSTRAINT ck_email_investigation_status CHECK (status IN ('OPEN','INVESTIGATING','WAITING_FOR_PROVIDER_EVIDENCE','CONFIRMED_ACCEPTED','CONFIRMED_NOT_ACCEPTED','INCONCLUSIVE','RESOLVED')),
    CONSTRAINT ck_email_investigation_outcome CHECK (resolution_outcome IS NULL OR resolution_outcome IN ('accepted','not_accepted','inconclusive'))
);
CREATE INDEX IF NOT EXISTS ix_email_delivery_investigation_message_id
    ON email_delivery_investigation (message_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_email_investigation_open_message
    ON email_delivery_investigation (message_id) WHERE status <> 'RESOLVED';

CREATE TABLE IF NOT EXISTS email_delivery_investigation_event (
    id SERIAL NOT NULL PRIMARY KEY,
    investigation_id INTEGER NOT NULL REFERENCES email_delivery_investigation(id) ON DELETE RESTRICT,
    actor_admin_id INTEGER NOT NULL REFERENCES admin_user(id) ON DELETE RESTRICT,
    event_type VARCHAR(32) NOT NULL,
    from_status VARCHAR(40),
    to_status VARCHAR(40),
    evidence_summary TEXT,
    evidence_source VARCHAR(255),
    explanation TEXT,
    related_attempt_id INTEGER REFERENCES email_delivery_attempt(id) ON DELETE RESTRICT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_email_delivery_investigation_event_investigation_id
    ON email_delivery_investigation_event (investigation_id);

CREATE TABLE IF NOT EXISTS email_worker_run (
    id SERIAL NOT NULL PRIMARY KEY,
    instance_id VARCHAR(36) NOT NULL UNIQUE,
    worker_type VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    last_heartbeat_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    last_success_at TIMESTAMP WITHOUT TIME ZONE,
    last_attempt_at TIMESTAMP WITHOUT TIME ZONE,
    current_batch_count INTEGER NOT NULL,
    stopped_at TIMESTAMP WITHOUT TIME ZONE,
    last_error_category VARCHAR(48),
    CONSTRAINT ck_email_worker_run_status CHECK (status IN ('STARTING','RUNNING','STOPPING','STOPPED','FAILED'))
);
CREATE INDEX IF NOT EXISTS ix_email_worker_run_last_heartbeat_at
    ON email_worker_run (last_heartbeat_at);
CREATE INDEX IF NOT EXISTS ix_email_worker_run_status_heartbeat
    ON email_worker_run (status, last_heartbeat_at);

-- PostgreSQL trigger DDL is created as a single transaction by the Python
-- migration so PL/pgSQL bodies are not split on their internal semicolons.
-- See apply_email_delivery_ops_migration() in app/services/email_delivery_ops.py.
