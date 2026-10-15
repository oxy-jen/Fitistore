-- Additive, idempotent PostgreSQL migration for referral profiles and rewards.
ALTER TABLE "order" ADD COLUMN IF NOT EXISTS referral_code VARCHAR(40) NOT NULL DEFAULT '';
ALTER TABLE "order" ADD COLUMN IF NOT EXISTS referrer_key VARCHAR(120) NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS ix_order_referral_code ON "order" (referral_code);
CREATE INDEX IF NOT EXISTS ix_order_referrer_key ON "order" (referrer_key);

ALTER TABLE shop_settings ADD COLUMN IF NOT EXISTS referral_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE shop_settings ADD COLUMN IF NOT EXISTS referral_reward_points INTEGER NOT NULL DEFAULT 50;
ALTER TABLE shop_settings ADD COLUMN IF NOT EXISTS referral_min_order_cents INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS customer_profile (
    id SERIAL PRIMARY KEY,
    phone VARCHAR(40) NOT NULL UNIQUE,
    display_name VARCHAR(120),
    referral_code VARCHAR(40) NOT NULL UNIQUE,
    referred_by_code VARCHAR(40),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_customer_profile_phone ON customer_profile (phone);
CREATE INDEX IF NOT EXISTS ix_customer_profile_referral_code ON customer_profile (referral_code);
CREATE INDEX IF NOT EXISTS ix_customer_profile_referred_by_code ON customer_profile (referred_by_code);

CREATE TABLE IF NOT EXISTS referral_click (
    id SERIAL PRIMARY KEY,
    referrer_id INTEGER NOT NULL REFERENCES customer_profile(id),
    referral_code VARCHAR(40) NOT NULL,
    visitor_key VARCHAR(96) NOT NULL,
    rewarded BOOLEAN NOT NULL DEFAULT FALSE,
    reward_points INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    CONSTRAINT uq_referral_click_visitor UNIQUE (referral_code, visitor_key)
);
CREATE INDEX IF NOT EXISTS ix_referral_click_referrer_id ON referral_click (referrer_id);
CREATE INDEX IF NOT EXISTS ix_referral_click_referral_code ON referral_click (referral_code);
CREATE INDEX IF NOT EXISTS ix_referral_click_visitor_key ON referral_click (visitor_key);
