-- Idempotent PostgreSQL migration for Cloudinary-backed product images.
-- Run only after taking a production database backup.
ALTER TABLE product_image
  ADD COLUMN IF NOT EXISTS storage_backend VARCHAR(20) NOT NULL DEFAULT 'local';

ALTER TABLE product_image
  ADD COLUMN IF NOT EXISTS cloudinary_public_id VARCHAR(255);