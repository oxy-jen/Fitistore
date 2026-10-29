-- Ordered, admin-curated products shown in the homepage Mamba collection.
CREATE TABLE IF NOT EXISTS mamba_collection (
    id SERIAL PRIMARY KEY,
    is_configured BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS mamba_collection_item (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL UNIQUE REFERENCES product(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0
);
