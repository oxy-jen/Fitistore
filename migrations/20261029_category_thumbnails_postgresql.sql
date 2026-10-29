-- Admin-managed homepage category card thumbnails.
CREATE TABLE IF NOT EXISTS home_category_thumbnail (
    id SERIAL PRIMARY KEY,
    group_key VARCHAR(50) NOT NULL UNIQUE,
    filename VARCHAR(255) NOT NULL DEFAULT '',
    storage_backend VARCHAR(20) NOT NULL DEFAULT 'local',
    cloudinary_public_id VARCHAR(255),
    alt_text VARCHAR(255) NOT NULL DEFAULT 'Category thumbnail',
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
