-- Additive metadata for idempotent, reviewable campaign draft preparation.
ALTER TABLE email_campaign ADD COLUMN IF NOT EXISTS generation_key VARCHAR(160);
ALTER TABLE email_campaign ADD COLUMN IF NOT EXISTS generation_source VARCHAR(32);
ALTER TABLE email_campaign ADD COLUMN IF NOT EXISTS generation_reason VARCHAR(255);
ALTER TABLE email_campaign ADD COLUMN IF NOT EXISTS generation_reference TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_email_campaign_generation_key
    ON email_campaign (generation_key)
    WHERE generation_key IS NOT NULL;
