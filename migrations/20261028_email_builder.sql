-- Additive visual-builder fields and allow multiple named templates per email category.
ALTER TABLE email_template ADD COLUMN IF NOT EXISTS name VARCHAR(120) NOT NULL DEFAULT 'Saved template';
ALTER TABLE email_template ADD COLUMN IF NOT EXISTS blocks_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE email_draft ADD COLUMN IF NOT EXISTS blocks_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE email_message ADD COLUMN IF NOT EXISTS template_id INTEGER;
CREATE INDEX IF NOT EXISTS ix_email_message_template_id ON email_message (template_id);
ALTER TABLE email_template DROP CONSTRAINT IF EXISTS email_template_category_key;
