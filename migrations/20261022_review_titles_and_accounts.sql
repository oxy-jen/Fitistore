ALTER TABLE review ADD COLUMN IF NOT EXISTS title VARCHAR(120) NOT NULL DEFAULT '';
ALTER TABLE review ADD COLUMN IF NOT EXISTS customer_account_id INTEGER;
CREATE INDEX IF NOT EXISTS ix_review_customer_account_id ON review(customer_account_id);
