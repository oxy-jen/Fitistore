ALTER TABLE support_conversation
    ADD COLUMN IF NOT EXISTS customer_first_name VARCHAR(80) NOT NULL DEFAULT '';
ALTER TABLE support_conversation
    ADD COLUMN IF NOT EXISTS customer_last_name VARCHAR(80) NOT NULL DEFAULT '';
ALTER TABLE support_conversation
    ADD COLUMN IF NOT EXISTS escalation_requested_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS support_attachment (
    id SERIAL PRIMARY KEY,
    message_id INTEGER NOT NULL UNIQUE
        REFERENCES support_message(id) ON DELETE CASCADE,
    filename VARCHAR(180) NOT NULL,
    content_type VARCHAR(80) NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 1 AND 3145728),
    data BYTEA NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
