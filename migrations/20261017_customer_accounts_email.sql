CREATE TABLE IF NOT EXISTS customer_account (
 id SERIAL PRIMARY KEY, email VARCHAR(254) NOT NULL UNIQUE, display_name VARCHAR(120) NOT NULL DEFAULT '', password_hash VARCHAR(255), google_sub VARCHAR(255) UNIQUE, email_verified_at TIMESTAMP, marketing_status VARCHAR(20) NOT NULL DEFAULT 'none', marketing_consent_at TIMESTAMP, marketing_withdrawn_at TIMESTAMP, active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_customer_account_email ON customer_account(email);
CREATE INDEX IF NOT EXISTS ix_customer_account_google_sub ON customer_account(google_sub);
CREATE INDEX IF NOT EXISTS ix_customer_account_marketing_status ON customer_account(marketing_status);
CREATE TABLE IF NOT EXISTS customer_token (
 id SERIAL PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES customer_account(id), purpose VARCHAR(24) NOT NULL, token_hash VARCHAR(64) NOT NULL UNIQUE, expires_at TIMESTAMP NOT NULL, used_at TIMESTAMP, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_customer_token_account_id ON customer_token(account_id);
CREATE INDEX IF NOT EXISTS ix_customer_token_purpose ON customer_token(purpose);
CREATE INDEX IF NOT EXISTS ix_customer_token_token_hash ON customer_token(token_hash);
CREATE INDEX IF NOT EXISTS ix_customer_token_expires_at ON customer_token(expires_at);
CREATE TABLE IF NOT EXISTS customer_consent_event (
 id SERIAL PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES customer_account(id), event VARCHAR(24) NOT NULL, method VARCHAR(24) NOT NULL, wording_version VARCHAR(32) NOT NULL DEFAULT '2026-10', created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_customer_consent_event_account_id ON customer_consent_event(account_id);
CREATE TABLE IF NOT EXISTS customer_order_link (
 id SERIAL PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES customer_account(id), order_id INTEGER NOT NULL UNIQUE REFERENCES "order"(id), created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_customer_order_link_account_id ON customer_order_link(account_id);
CREATE INDEX IF NOT EXISTS ix_customer_order_link_order_id ON customer_order_link(order_id);
CREATE TABLE IF NOT EXISTS auth_rate_limit (
 key VARCHAR(64) PRIMARY KEY, window_started_at TIMESTAMP NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS email_campaign (
 id SERIAL PRIMARY KEY, name VARCHAR(120) NOT NULL, subject VARCHAR(200) NOT NULL, body TEXT NOT NULL, status VARCHAR(24) NOT NULL DEFAULT 'draft', created_by_id INTEGER NOT NULL REFERENCES admin_user(id), sent_at TIMESTAMP, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_email_campaign_status ON email_campaign(status);
CREATE TABLE IF NOT EXISTS email_message (
 id SERIAL PRIMARY KEY, recipient VARCHAR(254) NOT NULL, account_id INTEGER REFERENCES customer_account(id), order_id INTEGER REFERENCES "order"(id), campaign_id INTEGER REFERENCES email_campaign(id), kind VARCHAR(32) NOT NULL, subject VARCHAR(200) NOT NULL, idempotency_key VARCHAR(160) NOT NULL UNIQUE, status VARCHAR(24) NOT NULL DEFAULT 'queued', failure_reason VARCHAR(255) NOT NULL DEFAULT '', sent_at TIMESTAMP, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_email_message_recipient ON email_message(recipient);
CREATE INDEX IF NOT EXISTS ix_email_message_account_id ON email_message(account_id);
CREATE INDEX IF NOT EXISTS ix_email_message_order_id ON email_message(order_id);
CREATE INDEX IF NOT EXISTS ix_email_message_campaign_id ON email_message(campaign_id);
CREATE INDEX IF NOT EXISTS ix_email_message_kind ON email_message(kind);
CREATE INDEX IF NOT EXISTS ix_email_message_status ON email_message(status);
CREATE TABLE IF NOT EXISTS email_campaign_recipient (
 id SERIAL PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES email_campaign(id), account_id INTEGER NOT NULL REFERENCES customer_account(id), recipient VARCHAR(254) NOT NULL, status VARCHAR(24) NOT NULL DEFAULT 'queued', failure_reason VARCHAR(255) NOT NULL DEFAULT '', sent_at TIMESTAMP, CONSTRAINT uq_campaign_recipient_account UNIQUE (campaign_id, account_id)
);
CREATE INDEX IF NOT EXISTS ix_email_campaign_recipient_campaign_id ON email_campaign_recipient(campaign_id);
CREATE INDEX IF NOT EXISTS ix_email_campaign_recipient_account_id ON email_campaign_recipient(account_id);
CREATE INDEX IF NOT EXISTS ix_email_campaign_recipient_status ON email_campaign_recipient(status);
