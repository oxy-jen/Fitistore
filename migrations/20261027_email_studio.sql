CREATE TABLE IF NOT EXISTS email_template (
    id SERIAL PRIMARY KEY,
    category VARCHAR(32) NOT NULL UNIQUE,
    subject VARCHAR(200) NOT NULL,
    headline VARCHAR(200) NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_email_template_category ON email_template (category);

CREATE TABLE IF NOT EXISTS email_draft (
    id SERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    category VARCHAR(32) NOT NULL,
    subject VARCHAR(200) NOT NULL,
    headline VARCHAR(200) NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    product_ids_json TEXT NOT NULL DEFAULT '[]',
    account_ids_json TEXT NOT NULL DEFAULT '[]',
    order_id INTEGER REFERENCES "order"(id),
    cart_id INTEGER REFERENCES customer_cart(id),
    status VARCHAR(24) NOT NULL DEFAULT 'draft',
    created_by_id INTEGER NOT NULL REFERENCES admin_user(id),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_email_draft_category ON email_draft (category);
CREATE INDEX IF NOT EXISTS ix_email_draft_order_id ON email_draft (order_id);
CREATE INDEX IF NOT EXISTS ix_email_draft_cart_id ON email_draft (cart_id);
CREATE INDEX IF NOT EXISTS ix_email_draft_status ON email_draft (status);
