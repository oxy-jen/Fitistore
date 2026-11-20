"""Explicit additive migration for isolated marketing campaign scheduling."""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import inspect, text

MIGRATION_ID = "20261120_marketing_campaign_queue"
_MIGRATION_FILE = Path(__file__).resolve().parents[2] / "migrations" / f"{MIGRATION_ID}.sql"
_COLUMNS = {
    "email_campaign": {
        "campaign_type": ("VARCHAR(32)", False, "'custom'"),
        "product_ids_json": ("TEXT", False, "'[]'"),
        "scheduled_for": ("TIMESTAMP WITHOUT TIME ZONE", True, None),
        "previewed_at": ("TIMESTAMP WITHOUT TIME ZONE", True, None),
        "approved_at": ("TIMESTAMP WITHOUT TIME ZONE", True, None),
    },
    "email_campaign_recipient": {
        "exclusion_reason": ("VARCHAR(48)", False, "''"),
        "message_id": ("INTEGER", True, None),
    },
}


def _normalise_default(value) -> str:
    expression = str(value or "").lower().strip()
    if "::" in expression:
        expression = expression.split("::", 1)[0].strip()
    return expression.strip("() '")


def migration_checksum() -> str:
    return hashlib.sha256(_MIGRATION_FILE.read_bytes()).hexdigest()


def apply_marketing_campaign_schema(connection, dialect: str) -> bool:
    if dialect not in {"sqlite", "postgresql", "postgres"}:
        raise ValueError("Unsupported marketing campaign migration dialect.")
    inspector = inspect(connection)
    if not {"email_campaign", "email_campaign_recipient"} <= set(inspector.get_table_names()):
        raise ValueError("Email campaign tables must exist before the marketing campaign migration.")
    prior = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:id"
    ), {"id": MIGRATION_ID}).scalar_one_or_none()
    checksum = migration_checksum()
    if prior is not None and prior != checksum:
        raise ValueError("Marketing campaign migration checksum differs from this code revision.")

    for table, columns in _COLUMNS.items():
        present = {row["name"] for row in inspect(connection).get_columns(table)}
        for name, (sql_type, nullable, default) in columns.items():
            if name in present:
                continue
            actual_type = sql_type.replace("TIMESTAMP WITHOUT TIME ZONE", "DATETIME") if dialect == "sqlite" else sql_type
            null_sql = "" if nullable else " NOT NULL"
            default_sql = f" DEFAULT {default}" if default is not None else ""
            connection.execute(text(
                f"ALTER TABLE {table} ADD COLUMN {name} {actual_type}{null_sql}{default_sql}"
            ))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_email_campaign_status_scheduled "
        "ON email_campaign (status, scheduled_for)"
    ))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_email_campaign_recipient_message "
        "ON email_campaign_recipient (message_id)"
    ))
    connection.execute(text(
        "INSERT INTO email_schema_migration (migration_id, checksum) VALUES (:id,:checksum) "
        "ON CONFLICT (migration_id) DO NOTHING"
    ), {"id": MIGRATION_ID, "checksum": checksum})
    validate_marketing_campaign_schema(connection)
    if inspect(connection).has_table("email_migration_history"):
        from app.services.email_migrations import record_migration
        record_migration(
            connection, MIGRATION_ID, state="applied",
            evidence="marketing campaign scheduling schema validated",
        )
    return prior is None


def validate_marketing_campaign_schema(connection) -> None:
    inspector = inspect(connection)
    for table, expected_columns in _COLUMNS.items():
        actual = {row["name"]: row for row in inspector.get_columns(table)}
        for name, (sql_type, nullable, default) in expected_columns.items():
            row = actual.get(name)
            if row is None:
                raise ValueError(f"Marketing campaign column {table}.{name} is missing.")
            actual_type = row["type"].compile(dialect=connection.dialect).upper()
            expected_type = sql_type if connection.dialect.name == "postgresql" else (
                "DATETIME" if sql_type.startswith("TIMESTAMP") else sql_type
            )
            if actual_type != expected_type or bool(row["nullable"]) != nullable:
                raise ValueError(f"Marketing campaign column {table}.{name} is incompatible.")
            if default is not None:
                if _normalise_default(row.get("default")) != _normalise_default(default):
                    raise ValueError(f"Marketing campaign column {table}.{name} has an incompatible default.")
    indexes = {
        (row["name"], tuple(row.get("column_names") or ()))
        for table in _COLUMNS
        for row in inspector.get_indexes(table)
    }
    if ("ix_email_campaign_status_scheduled", ("status", "scheduled_for")) not in indexes:
        raise ValueError("Marketing campaign scheduling index is missing.")
    if ("ix_email_campaign_recipient_message", ("message_id",)) not in indexes:
        raise ValueError("Marketing campaign recipient message index is missing.")
    checksum = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:id"
    ), {"id": MIGRATION_ID}).scalar_one_or_none()
    if checksum != migration_checksum():
        raise ValueError("Marketing campaign migration checksum is missing or incompatible.")
