-- Schema contract for email_legacy_payload_review_event; created in one
-- caller-owned transaction by app.services.email_legacy_reviews.
CREATE TABLE IF NOT EXISTS email_legacy_payload_review_event (
    id SERIAL PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES email_message(id) ON DELETE RESTRICT,
    actor_admin_id INTEGER REFERENCES admin_user(id) ON DELETE RESTRICT,
    actor_type VARCHAR(16) NOT NULL,
    action VARCHAR(24) NOT NULL DEFAULT 'quarantined',
    reason TEXT NOT NULL,
    provider_outcome VARCHAR(32) NOT NULL DEFAULT 'unknown',
    provider_evidence TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    CONSTRAINT ck_email_legacy_review_actor CHECK ((actor_type = 'admin' AND actor_admin_id IS NOT NULL) OR (actor_type = 'system' AND actor_admin_id IS NULL)),
    CONSTRAINT ck_email_legacy_review_action CHECK (action IN ('quarantined', 'reviewed')),
    CONSTRAINT ck_email_legacy_review_provider_outcome CHECK (provider_outcome IN ('unknown', 'confirmed_not_accepted', 'confirmed_accepted'))
);
CREATE INDEX IF NOT EXISTS ix_email_legacy_review_message_created ON email_legacy_payload_review_event (message_id, created_at);
-- PostgreSQL and SQLite append-only trigger definitions are installed by the
-- Python migration; Python does not split procedural bodies on semicolons.
