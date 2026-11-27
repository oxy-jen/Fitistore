CREATE TABLE IF NOT EXISTS support_conversation (
    id SERIAL PRIMARY KEY,
    public_id VARCHAR(32) NOT NULL UNIQUE,
    account_id INTEGER REFERENCES customer_account(id) ON DELETE CASCADE,
    guest_token_hash VARCHAR(64),
    topic VARCHAR(24) NOT NULL DEFAULT 'general',
    status VARCHAR(20) NOT NULL DEFAULT 'waiting',
    assigned_admin_id INTEGER REFERENCES admin_user(id) ON DELETE SET NULL,
    notification_status VARCHAR(20) NOT NULL DEFAULT 'not_configured',
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_message_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_support_conversation_has_owner
        CHECK (account_id IS NOT NULL OR guest_token_hash IS NOT NULL),
    CONSTRAINT ck_support_conversation_topic
        CHECK (topic IN ('general', 'orders', 'payments', 'delivery', 'returns', 'products')),
    CONSTRAINT ck_support_conversation_status
        CHECK (status IN ('waiting', 'assigned', 'closed')),
    CONSTRAINT ck_support_conversation_notification_status
        CHECK (notification_status IN ('not_configured', 'queued', 'failed'))
);

CREATE INDEX IF NOT EXISTS ix_support_conversation_queue
    ON support_conversation (status, created_at);
CREATE INDEX IF NOT EXISTS ix_support_conversation_account_status
    ON support_conversation (account_id, status);
CREATE INDEX IF NOT EXISTS ix_support_conversation_guest_token_hash
    ON support_conversation (guest_token_hash);
CREATE INDEX IF NOT EXISTS ix_support_conversation_assigned_admin_id
    ON support_conversation (assigned_admin_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_support_open_conversation_account
    ON support_conversation (account_id)
    WHERE account_id IS NOT NULL AND status <> 'closed';
CREATE UNIQUE INDEX IF NOT EXISTS uq_support_open_conversation_guest
    ON support_conversation (guest_token_hash)
    WHERE guest_token_hash IS NOT NULL AND status <> 'closed';

CREATE TABLE IF NOT EXISTS support_message (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL
        REFERENCES support_conversation(id) ON DELETE CASCADE,
    sender_type VARCHAR(12) NOT NULL,
    sender_account_id INTEGER REFERENCES customer_account(id) ON DELETE SET NULL,
    sender_admin_id INTEGER REFERENCES admin_user(id) ON DELETE SET NULL,
    sender_name VARCHAR(100) NOT NULL,
    client_message_id VARCHAR(36) NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_support_message_sender_type
        CHECK (sender_type IN ('customer', 'agent')),
    CONSTRAINT ck_support_message_body_length
        CHECK (length(trim(body)) BETWEEN 1 AND 2000),
    CONSTRAINT uq_support_message_client_id
        UNIQUE (conversation_id, client_message_id)
);
CREATE INDEX IF NOT EXISTS ix_support_message_conversation_id
    ON support_message (conversation_id, id);

CREATE TABLE IF NOT EXISTS support_agent_presence (
    admin_id INTEGER PRIMARY KEY REFERENCES admin_user(id) ON DELETE CASCADE,
    status VARCHAR(16) NOT NULL DEFAULT 'away',
    heartbeat_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_support_agent_presence_status
        CHECK (status IN ('available', 'busy', 'away'))
);
CREATE INDEX IF NOT EXISTS ix_support_agent_presence_heartbeat
    ON support_agent_presence (heartbeat_at);
