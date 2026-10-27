-- Explicit consent copy identifiers exceed the original 32-character limit.
ALTER TABLE customer_consent_event
    ALTER COLUMN wording_version TYPE VARCHAR(120);
