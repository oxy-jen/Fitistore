-- Additive campaign scheduling and recipient linkage; existing campaigns remain drafts.
ALTER TABLE email_campaign ADD COLUMN IF NOT EXISTS campaign_type VARCHAR(32) NOT NULL DEFAULT 'custom';
ALTER TABLE email_campaign ADD COLUMN IF NOT EXISTS product_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE email_campaign ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE email_campaign ADD COLUMN IF NOT EXISTS previewed_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE email_campaign ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE email_campaign_recipient ADD COLUMN IF NOT EXISTS exclusion_reason VARCHAR(48) NOT NULL DEFAULT '';
ALTER TABLE email_campaign_recipient ADD COLUMN IF NOT EXISTS message_id INTEGER;
CREATE INDEX IF NOT EXISTS ix_email_campaign_status_scheduled
    ON email_campaign (status, scheduled_for);
CREATE INDEX IF NOT EXISTS ix_email_campaign_recipient_message
    ON email_campaign_recipient (message_id);
