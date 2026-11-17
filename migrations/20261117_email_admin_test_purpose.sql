-- Migration ID: 20261117_email_admin_test_purpose
-- Additive data correction executed by app.services.email_test_purpose_migration.
-- It updates only legacy kind='admin_test' rows whose purpose is not 'test'.
-- The application migration runner executes this in its existing transaction
-- and advisory-lock boundary, then records this file's SHA-256 checksum.
UPDATE email_message
SET purpose = 'test'
WHERE kind = 'admin_test' AND purpose <> 'test';
