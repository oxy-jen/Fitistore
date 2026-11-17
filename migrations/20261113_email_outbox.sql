-- Versioned, additive outbox migration contract.
-- The explicit Flask migration command executes equivalent individual DDL
-- statements in one transaction, records this file's SHA-256, and validates it.
ALTER TABLE email_message ADD COLUMN IF NOT EXISTS purpose VARCHAR(20) NOT NULL DEFAULT 'transactional';
ALTER TABLE email_message ADD COLUMN IF NOT EXISTS frequency_override BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE email_message ADD COLUMN IF NOT EXISTS html_body TEXT;
ALTER TABLE email_message ADD COLUMN IF NOT EXISTS text_body TEXT;
ALTER TABLE email_message ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE email_message ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE email_message ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE email_message ADD COLUMN IF NOT EXISTS error_category VARCHAR(48);
ALTER TABLE email_message ADD COLUMN IF NOT EXISTS claim_token VARCHAR(36);
ALTER TABLE marketing_campaign_run ADD COLUMN IF NOT EXISTS queued_count INTEGER NOT NULL DEFAULT 0;

-- Backfill only values left null or incorrectly defaulted in legacy schemas.
UPDATE email_message SET purpose = CASE
  WHEN kind = 'marketing_campaign'
    OR (kind LIKE 'marketing_%' AND kind NOT IN ('marketing_confirm', 'marketing_confirmation'))
    OR (kind LIKE 'admin_%' AND kind <> 'admin_test') THEN 'marketing'
  ELSE 'transactional'
END
WHERE purpose IS NULL OR purpose NOT IN ('marketing', 'transactional')
  OR (purpose = 'transactional' AND (
      kind = 'marketing_campaign'
      OR (kind LIKE 'marketing_%' AND kind NOT IN ('marketing_confirm', 'marketing_confirmation'))
      OR (kind LIKE 'admin_%' AND kind <> 'admin_test')));

UPDATE email_message SET frequency_override = FALSE WHERE frequency_override IS DISTINCT FROM FALSE;
CREATE INDEX IF NOT EXISTS ix_email_message_purpose ON email_message (purpose);
CREATE INDEX IF NOT EXISTS ix_email_message_dispatch ON email_message (status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS ix_email_message_claimed_at ON email_message (claimed_at);
CREATE INDEX IF NOT EXISTS ix_email_message_next_attempt_at ON email_message (next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_email_message_claim_token ON email_message (claim_token);
