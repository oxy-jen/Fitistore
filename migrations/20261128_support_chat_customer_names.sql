ALTER TABLE support_conversation
    ADD COLUMN IF NOT EXISTS customer_first_name VARCHAR(80) NOT NULL DEFAULT '';
ALTER TABLE support_conversation
    ADD COLUMN IF NOT EXISTS customer_last_name VARCHAR(80) NOT NULL DEFAULT '';
ALTER TABLE support_conversation
    ADD COLUMN IF NOT EXISTS escalation_requested_at TIMESTAMP;
