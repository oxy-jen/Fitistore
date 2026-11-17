"""Correct the legacy admin_test classification without rewriting old migrations."""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import inspect, text

MIGRATION_ID = "20261117_email_admin_test_purpose"
_MIGRATION_FILE = Path(__file__).resolve().parents[2] / "migrations" / f"{MIGRATION_ID}.sql"


def migration_checksum() -> str:
    return hashlib.sha256(_MIGRATION_FILE.read_bytes()).hexdigest()


def validate_email_test_purpose(connection) -> None:
    if not inspect(connection).has_table("email_message"):
        raise ValueError("Email message table is missing.")
    columns = {column["name"] for column in inspect(connection).get_columns("email_message")}
    if not {"kind", "purpose"} <= columns:
        raise ValueError("Email message purpose columns are incomplete.")
    count = connection.execute(text(
        "SELECT count(*) FROM email_message WHERE kind='admin_test' AND purpose <> 'test'"
    )).scalar_one()
    if count:
        raise ValueError("Legacy admin_test messages are not classified as test-only.")
    recorded = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:id"
    ), {"id": MIGRATION_ID}).scalar_one_or_none()
    if recorded != migration_checksum():
        raise ValueError("Email test-purpose migration checksum is missing or incompatible.")


def apply_email_test_purpose_migration(connection, dialect: str) -> bool:
    """Apply a narrowly scoped additive data correction in the caller transaction."""
    if dialect not in {"sqlite", "postgresql", "postgres"}:
        raise ValueError("Unsupported email test-purpose migration dialect.")
    inspector = inspect(connection)
    if not inspector.has_table("email_message") or not inspector.has_table("email_schema_migration"):
        raise ValueError("Email outbox and its migration ledger must exist first.")
    checksum = migration_checksum()
    prior = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:id"
    ), {"id": MIGRATION_ID}).scalar_one_or_none()
    if prior is not None and prior != checksum:
        raise ValueError("Recorded email test-purpose migration checksum differs from this code revision.")
    if prior is None:
        connection.execute(text(
            "UPDATE email_message SET purpose='test' WHERE kind='admin_test' AND purpose <> 'test'"
        ))
        connection.execute(text(
            "INSERT INTO email_schema_migration (migration_id, checksum) VALUES (:id, :checksum)"
        ), {"id": MIGRATION_ID, "checksum": checksum})
    validate_email_test_purpose(connection)
    from app.services.email_migrations import history_rows, record_migration
    if history_rows(connection):
        record_migration(connection, MIGRATION_ID, state="applied",
                         evidence="legacy admin_test outbox purpose corrected and validated")
    return prior is None
