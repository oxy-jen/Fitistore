-- Additive Media Showcases feature. Safe to run repeatedly; existing store rows are untouched.
CREATE TABLE IF NOT EXISTS media_showcase (
    id SERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    title VARCHAR(160) NOT NULL DEFAULT '',
    subtitle VARCHAR(200) NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    slug VARCHAR(150) NOT NULL UNIQUE,
    page_identifier VARCHAR(40) NOT NULL,
    placement_identifier VARCHAR(80) NOT NULL,
    layout VARCHAR(40) NOT NULL DEFAULT 'carousel' CHECK (layout IN ('carousel','banner','grid','featured','mixed','single','split')),
    density VARCHAR(16) NOT NULL DEFAULT 'compact' CHECK (density IN ('compact','balanced','spacious')),
    card_aspect_ratio VARCHAR(8) NOT NULL DEFAULT '4:3' CHECK (card_aspect_ratio IN ('1:1','4:3','3:2','16:9')),
    visible_cards_mobile INTEGER NOT NULL DEFAULT 2 CHECK (visible_cards_mobile BETWEEN 1 AND 4),
    visible_cards_tablet INTEGER NOT NULL DEFAULT 3 CHECK (visible_cards_tablet BETWEEN 1 AND 5),
    visible_cards_desktop INTEGER NOT NULL DEFAULT 4 CHECK (visible_cards_desktop BETWEEN 1 AND 6),
    section_padding_px INTEGER CHECK (section_padding_px IS NULL OR section_padding_px BETWEEN 0 AND 40),
    card_gap_px INTEGER CHECK (card_gap_px IS NULL OR card_gap_px BETWEEN 0 AND 40),
    heading_color VARCHAR(20) NOT NULL DEFAULT '#252525',
    description_color VARCHAR(20) NOT NULL DEFAULT '#666666',
    accent_color VARCHAR(20) NOT NULL DEFAULT '#e52335',
    card_background_color VARCHAR(20) NOT NULL DEFAULT '#ffffff',
    border_color VARCHAR(20) NOT NULL DEFAULT '#e8e8eb',
    button_background_color VARCHAR(20) NOT NULL DEFAULT '#e52335',
    button_text_color VARCHAR(20) NOT NULL DEFAULT '#ffffff',
    display_order INTEGER NOT NULL DEFAULT 0 CHECK (display_order >= 0),
    is_published BOOLEAN NOT NULL DEFAULT FALSE,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    starts_at TIMESTAMP WITHOUT TIME ZONE,
    expires_at TIMESTAMP WITHOUT TIME ZONE,
    background_color VARCHAR(20) NOT NULL DEFAULT '',
    created_by_id INTEGER REFERENCES admin_user(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_media_showcase_name ON media_showcase (name);
CREATE INDEX IF NOT EXISTS ix_media_showcase_slug ON media_showcase (slug);
CREATE INDEX IF NOT EXISTS ix_media_showcase_page_identifier ON media_showcase (page_identifier);
CREATE INDEX IF NOT EXISTS ix_media_showcase_placement_identifier ON media_showcase (placement_identifier);
CREATE INDEX IF NOT EXISTS ix_media_showcase_is_published ON media_showcase (is_published);
CREATE INDEX IF NOT EXISTS ix_media_showcase_is_enabled ON media_showcase (is_enabled);

-- Add these presentation controls when upgrading an already-created showcase table.
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS density VARCHAR(16) NOT NULL DEFAULT 'compact';
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS card_aspect_ratio VARCHAR(8) NOT NULL DEFAULT '4:3';
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS visible_cards_mobile INTEGER NOT NULL DEFAULT 2;
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS visible_cards_tablet INTEGER NOT NULL DEFAULT 3;
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS visible_cards_desktop INTEGER NOT NULL DEFAULT 4;
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS section_padding_px INTEGER;
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS card_gap_px INTEGER;
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS heading_color VARCHAR(20) NOT NULL DEFAULT '#252525';
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS description_color VARCHAR(20) NOT NULL DEFAULT '#666666';
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS accent_color VARCHAR(20) NOT NULL DEFAULT '#e52335';
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS card_background_color VARCHAR(20) NOT NULL DEFAULT '#ffffff';
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS border_color VARCHAR(20) NOT NULL DEFAULT '#e8e8eb';
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS button_background_color VARCHAR(20) NOT NULL DEFAULT '#e52335';
ALTER TABLE media_showcase ADD COLUMN IF NOT EXISTS button_text_color VARCHAR(20) NOT NULL DEFAULT '#ffffff';

CREATE TABLE IF NOT EXISTS media_showcase_item (
    id SERIAL PRIMARY KEY,
    showcase_id INTEGER NOT NULL REFERENCES media_showcase(id) ON DELETE CASCADE,
    media_type VARCHAR(20) NOT NULL CHECK (media_type IN ('video','gif','image','link')),
    title VARCHAR(160) NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    media_url VARCHAR(1000) NOT NULL DEFAULT '',
    poster_url VARCHAR(1000) NOT NULL DEFAULT '',
    thumbnail_url VARCHAR(1000) NOT NULL DEFAULT '',
    destination_url VARCHAR(1000) NOT NULL DEFAULT '',
    linked_product_id INTEGER REFERENCES product(id) ON DELETE SET NULL,
    cta_label VARCHAR(60) NOT NULL DEFAULT '',
    alt_text VARCHAR(255) NOT NULL DEFAULT '',
    display_order INTEGER NOT NULL DEFAULT 0 CHECK (display_order >= 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    autoplay BOOLEAN NOT NULL DEFAULT TRUE,
    muted BOOLEAN NOT NULL DEFAULT TRUE,
    loop BOOLEAN NOT NULL DEFAULT TRUE,
    show_controls BOOLEAN NOT NULL DEFAULT FALSE,
    presentation_mode VARCHAR(16) NOT NULL DEFAULT 'original' CHECK (presentation_mode IN ('original','contain','cover')),
    aspect_ratio VARCHAR(24) NOT NULL DEFAULT '',
    source_type VARCHAR(20) NOT NULL DEFAULT 'external_url',
    source_filename VARCHAR(255) NOT NULL DEFAULT '',
    file_size_bytes INTEGER,
    media_width INTEGER,
    media_height INTEGER,
    duration_seconds DOUBLE PRECISION,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE media_showcase_item ADD COLUMN IF NOT EXISTS presentation_mode VARCHAR(16) NOT NULL DEFAULT 'original';
ALTER TABLE media_showcase_item ADD COLUMN IF NOT EXISTS aspect_ratio VARCHAR(24) NOT NULL DEFAULT '';
ALTER TABLE media_showcase_item ADD COLUMN IF NOT EXISTS source_type VARCHAR(20) NOT NULL DEFAULT 'external_url';
ALTER TABLE media_showcase_item ADD COLUMN IF NOT EXISTS source_filename VARCHAR(255) NOT NULL DEFAULT '';
ALTER TABLE media_showcase_item ADD COLUMN IF NOT EXISTS file_size_bytes INTEGER;
ALTER TABLE media_showcase_item ADD COLUMN IF NOT EXISTS media_width INTEGER;
ALTER TABLE media_showcase_item ADD COLUMN IF NOT EXISTS media_height INTEGER;
ALTER TABLE media_showcase_item ADD COLUMN IF NOT EXISTS duration_seconds DOUBLE PRECISION;
UPDATE media_showcase_item SET source_type = 'library' WHERE media_url LIKE '/static/%';
UPDATE media_showcase_item SET source_type = 'upload' WHERE media_url LIKE '/static/uploads/media-showcases/%';
UPDATE media_showcase_item SET source_type = 'cloudinary' WHERE media_url LIKE '%res.cloudinary.com/%';
CREATE INDEX IF NOT EXISTS ix_media_showcase_item_showcase_id ON media_showcase_item (showcase_id);

-- A ready-to-edit draft reserves the requested first homepage slot without inventing media.
INSERT INTO media_showcase (name, title, subtitle, slug, page_identifier, placement_identifier, layout, display_order, is_published, is_enabled)
VALUES ('FityStores TV', 'FityStores TV', 'See it. Love it. Shop it.', 'fitystores-tv', 'home', 'home_after_hero', 'carousel', 0, FALSE, TRUE)
ON CONFLICT (slug) DO NOTHING;
