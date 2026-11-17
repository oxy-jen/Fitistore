-- Central checksum/evidence ledger for the existing customer/email migrations.
-- The explicit Python migration executes equivalent DDL in the caller-owned
-- transaction. Existing legacy schemas require an explicit schema-reconcile
-- step before this ledger can classify their historical migrations.
CREATE TABLE IF NOT EXISTS email_migration_history (
    migration_id VARCHAR(120) PRIMARY KEY,
    checksum VARCHAR(64) NOT NULL,
    state VARCHAR(24) NOT NULL CONSTRAINT ck_email_migration_history_state CHECK (state IN ('applied', 'reconciled_schema')),
    evidence TEXT NOT NULL DEFAULT '',
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
