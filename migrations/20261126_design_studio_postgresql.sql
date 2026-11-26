-- Additive FityStores Design Studio schema. Existing Media Showcase tables/data
-- are intentionally untouched. The Flask migration command uses the same
-- model metadata for SQLite and PostgreSQL and can be safely rerun.
CREATE TABLE IF NOT EXISTS design_studio_role (
    id SERIAL PRIMARY KEY,
    admin_user_id INTEGER NOT NULL UNIQUE REFERENCES admin_user(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL DEFAULT 'designer'
        CHECK (role IN ('owner','designer','editor','approver','publisher','viewer')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS storefront_design (
    id SERIAL PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    title VARCHAR(160) NOT NULL DEFAULT '',
    description VARCHAR(500) NOT NULL DEFAULT '',
    placement VARCHAR(32) NOT NULL DEFAULT 'storefront_banner'
        CHECK (placement IN ('storefront_banner','storefront_section')),
    status VARCHAR(16) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','pending','approved','published')),
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    starts_at TIMESTAMP,
    expires_at TIMESTAMP,
    html TEXT NOT NULL DEFAULT '',
    css TEXT NOT NULL DEFAULT '',
    javascript TEXT NOT NULL DEFAULT '',
    lottie_json TEXT NOT NULL DEFAULT '',
    product_ids TEXT NOT NULL DEFAULT '[]',
    template_key VARCHAR(40) NOT NULL DEFAULT 'editorial',
    motion_preset VARCHAR(20) NOT NULL DEFAULT 'none',
    motion_config TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_by_id INTEGER REFERENCES admin_user(id) ON DELETE SET NULL,
    updated_by_id INTEGER REFERENCES admin_user(id) ON DELETE SET NULL,
    approved_by_id INTEGER REFERENCES admin_user(id) ON DELETE SET NULL,
    published_by_id INTEGER REFERENCES admin_user(id) ON DELETE SET NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_storefront_design_publish
    ON storefront_design (status, is_enabled, placement);

CREATE TABLE IF NOT EXISTS storefront_design_revision (
    id SERIAL PRIMARY KEY,
    design_id INTEGER NOT NULL REFERENCES storefront_design(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    created_by_id INTEGER REFERENCES admin_user(id) ON DELETE SET NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_storefront_design_revision_number UNIQUE (design_id, revision_number)
);
CREATE INDEX IF NOT EXISTS ix_storefront_design_revision_design_id
    ON storefront_design_revision (design_id);

CREATE TABLE IF NOT EXISTS storefront_design_audit (
    id SERIAL PRIMARY KEY,
    design_id INTEGER REFERENCES storefront_design(id) ON DELETE SET NULL,
    admin_user_id INTEGER REFERENCES admin_user(id) ON DELETE SET NULL,
    action VARCHAR(32) NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_storefront_design_audit_design_id
    ON storefront_design_audit (design_id);
CREATE INDEX IF NOT EXISTS ix_storefront_design_audit_admin_user_id
    ON storefront_design_audit (admin_user_id);
CREATE INDEX IF NOT EXISTS ix_storefront_design_audit_created_at
    ON storefront_design_audit (created_at);

INSERT INTO design_studio_role (admin_user_id, role)
SELECT candidate.id,
       CASE
           WHEN candidate.id = (SELECT MIN(id) FROM admin_user)
                AND NOT EXISTS (SELECT 1 FROM design_studio_role WHERE role = 'owner')
           THEN 'owner'
           ELSE 'designer'
       END
FROM admin_user AS candidate
ON CONFLICT (admin_user_id) DO NOTHING;

UPDATE design_studio_role
SET role = 'owner', updated_at = CURRENT_TIMESTAMP
WHERE admin_user_id = (SELECT MIN(id) FROM admin_user)
  AND NOT EXISTS (SELECT 1 FROM design_studio_role WHERE role = 'owner');
