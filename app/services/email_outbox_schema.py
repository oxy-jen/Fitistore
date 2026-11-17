"""Explicit, additive migration and catalog validation for the email outbox."""

from __future__ import annotations

import hashlib
from pathlib import Path

import sqlalchemy
from sqlalchemy import text

MIGRATION_ID = "20261113_email_outbox"
_MIGRATION_FILE = Path(__file__).resolve().parents[2] / "migrations" / f"{MIGRATION_ID}.sql"
_INDEXES = {
    "ix_email_message_purpose": ("purpose",),
    "ix_email_message_dispatch": ("status", "next_attempt_at", "created_at"),
    "ix_email_message_claimed_at": ("claimed_at",),
    "ix_email_message_next_attempt_at": ("next_attempt_at",),
    "ix_email_message_claim_token": ("claim_token",),
}
_COLUMN_SQL = {
    "purpose": "VARCHAR(20) NOT NULL DEFAULT 'transactional'",
    "frequency_override": "BOOLEAN NOT NULL DEFAULT FALSE",
    "html_body": "TEXT",
    "text_body": "TEXT",
    "claimed_at": "TIMESTAMP WITHOUT TIME ZONE",
    "last_attempt_at": "TIMESTAMP WITHOUT TIME ZONE",
    "next_attempt_at": "TIMESTAMP WITHOUT TIME ZONE",
    "error_category": "VARCHAR(48)",
    "claim_token": "VARCHAR(36)",
}


def migration_checksum() -> str:
    return hashlib.sha256(_MIGRATION_FILE.read_bytes()).hexdigest()


def _ensure_ledger(connection) -> None:
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS email_schema_migration ("
        "migration_id VARCHAR(120) PRIMARY KEY, checksum VARCHAR(64) NOT NULL, "
        "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    ))


def _purpose_expression() -> str:
    return (
        "CASE WHEN (kind = 'marketing_campaign' OR "
        "(kind LIKE 'marketing_%' AND kind NOT IN ('marketing_confirm','marketing_confirmation')) OR "
        "(kind LIKE 'admin_%' AND kind <> 'admin_test')) "
        "THEN 'marketing' ELSE 'transactional' END"
    )


def apply_email_outbox_migration(connection, dialect: str) -> bool:
    """Apply individually executed additive DDL in caller-owned transaction."""
    if dialect not in {"sqlite", "postgresql", "postgres"}:
        raise ValueError("Unsupported email outbox migration dialect.")
    _ensure_ledger(connection)
    checksum = migration_checksum()
    prior = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:migration_id"
    ), {"migration_id": MIGRATION_ID}).scalar_one_or_none()
    if prior is not None:
        if prior != checksum:
            raise ValueError("Recorded email outbox migration checksum differs from this code revision.")
        validate_email_outbox_schema(connection)
        from app.services.email_migrations import history_rows, record_migration
        if history_rows(connection):
            record_migration(connection, MIGRATION_ID, state="applied", evidence="outbox checksum and schema validator passed")
        return False

    inspector = sqlalchemy.inspect(connection)
    if not inspector.has_table("email_message"):
        raise ValueError("email_message table must exist before the outbox migration.")
    columns = {column["name"] for column in inspector.get_columns("email_message")}
    for name, definition in _COLUMN_SQL.items():
        if name not in columns:
            # SQLite reports SQLAlchemy DateTime columns as DATETIME. Keep the
            # additive upgrade compatible with the same type contract rather
            # than leaving a PostgreSQL-only timestamp declaration behind.
            sqlite_definition = definition.replace("TIMESTAMP WITHOUT TIME ZONE", "DATETIME")
            column_definition = sqlite_definition if dialect == "sqlite" else definition
            connection.execute(text(f"ALTER TABLE email_message ADD COLUMN {name} {column_definition}"))
    inspector = sqlalchemy.inspect(connection)
    run_columns = {column["name"] for column in inspector.get_columns("marketing_campaign_run")} if inspector.has_table("marketing_campaign_run") else set()
    if "queued_count" not in run_columns:
        if not run_columns:
            raise ValueError("marketing_campaign_run table must exist before the outbox migration.")
        connection.execute(text("ALTER TABLE marketing_campaign_run ADD COLUMN queued_count INTEGER NOT NULL DEFAULT 0"))

    # Only normalize genuinely unset/misclassified legacy values; repeated runs
    # are protected by the checksum ledger and never rewrite message history.
    connection.execute(text(
        "UPDATE email_message SET purpose = " + _purpose_expression() +
        " WHERE purpose IS NULL OR purpose NOT IN ('marketing','transactional','test') "
        "OR (purpose='transactional' AND (kind='marketing_campaign' OR "
        "(kind LIKE 'marketing_%' AND kind NOT IN ('marketing_confirm','marketing_confirmation')) OR "
        "(kind LIKE 'admin_%' AND kind <> 'admin_test')))"
    ))
    connection.execute(text("UPDATE email_message SET frequency_override=FALSE WHERE frequency_override IS DISTINCT FROM FALSE" if dialect in {"postgres", "postgresql"} else "UPDATE email_message SET frequency_override=0 WHERE frequency_override<>0"))
    for name, columns_ in _INDEXES.items():
        joined = ", ".join(columns_)
        connection.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON email_message ({joined})"))
    if dialect in {"postgres", "postgresql"}:
        connection.execute(text("ALTER TABLE email_message ALTER COLUMN purpose SET DEFAULT 'transactional'"))
        connection.execute(text("ALTER TABLE email_message ALTER COLUMN purpose SET NOT NULL"))
        connection.execute(text("ALTER TABLE marketing_campaign_run ALTER COLUMN queued_count SET DEFAULT 0"))
    connection.execute(text(
        "INSERT INTO email_schema_migration (migration_id, checksum) VALUES (:migration_id, :checksum)"
    ), {"migration_id": MIGRATION_ID, "checksum": checksum})
    validate_email_outbox_schema(connection)
    from app.services.email_migrations import history_rows, record_migration
    if history_rows(connection):
        record_migration(connection, MIGRATION_ID, state="applied", evidence="outbox migration applied and schema validator passed")
    return True


def validate_email_outbox_schema(connection, *, schema: str | None = None) -> list[str]:
    inspector = sqlalchemy.inspect(connection)
    schema_arg = schema if connection.dialect.name == "postgresql" else None
    if not inspector.has_table("email_message", schema=schema_arg) or not inspector.has_table("marketing_campaign_run", schema=schema_arg):
        raise ValueError("Required email outbox or campaign-run table is missing.")
    email_columns = {column["name"]: column for column in inspector.get_columns("email_message", schema=schema_arg)}
    run_columns = {column["name"]: column for column in inspector.get_columns("marketing_campaign_run", schema=schema_arg)}
    required = set(_COLUMN_SQL) | {"id", "status", "kind", "created_at", "provider_message_id", "retry_count"}
    if missing := required - set(email_columns):
        raise ValueError("Email outbox columns are missing: " + ", ".join(sorted(missing)))
    if "queued_count" not in run_columns:
        raise ValueError("marketing_campaign_run.queued_count is missing.")
    for name in ("purpose", "frequency_override"):
        if email_columns[name].get("nullable", True):
            raise ValueError(f"email_message.{name} must be NOT NULL.")
    if run_columns["queued_count"].get("nullable", True):
        raise ValueError("marketing_campaign_run.queued_count must be NOT NULL.")
    purpose_default = str(email_columns["purpose"].get("default") or "").lower()
    frequency_default = str(email_columns["frequency_override"].get("default") or "").lower()
    queued_default = str(run_columns["queued_count"].get("default") or "").lower()
    if "transactional" not in purpose_default or not frequency_default.strip("() '").startswith(("false", "0")):
        raise ValueError("Email outbox purpose or frequency defaults are incompatible.")
    if not queued_default.strip("() ' ").startswith("0"):
        raise ValueError("Campaign queued_count default is incompatible.")
    queued_type = run_columns["queued_count"]["type"].compile(dialect=connection.dialect).upper()
    if queued_type != "INTEGER":
        raise ValueError("marketing_campaign_run.queued_count has an incompatible type.")
    expected_types = {
        "purpose": "VARCHAR(20)", "frequency_override": "BOOLEAN",
        "html_body": "TEXT", "text_body": "TEXT", "error_category": "VARCHAR(48)",
        "claim_token": "VARCHAR(36)",
        "claimed_at": "TIMESTAMP WITHOUT TIME ZONE" if connection.dialect.name == "postgresql" else "DATETIME",
        "last_attempt_at": "TIMESTAMP WITHOUT TIME ZONE" if connection.dialect.name == "postgresql" else "DATETIME",
        "next_attempt_at": "TIMESTAMP WITHOUT TIME ZONE" if connection.dialect.name == "postgresql" else "DATETIME",
    }
    for name, expected in expected_types.items():
        actual = email_columns[name]["type"].compile(dialect=connection.dialect).upper()
        if " ".join(actual.split()) != expected:
            raise ValueError(f"email_message.{name} has an incompatible database type.")
    indexes = {row["name"]: row for row in inspector.get_indexes("email_message", schema=schema_arg)}
    for name, columns in _INDEXES.items():
        row = indexes.get(name)
        dialect_options = row.get("dialect_options") or {} if row else {}
        if (not row or tuple(row.get("column_names") or ()) != columns or row.get("unique")
                or dialect_options.get("postgresql_where") or dialect_options.get("sqlite_where")):
            raise ValueError(f"Email outbox index {name} is missing or incompatible.")
    if connection.dialect.name == "postgresql":
        for name in _INDEXES:
            state = connection.execute(text(
                "SELECT i.indisvalid, i.indisready, i.indisunique, i.indpred IS NULL "
                "FROM pg_index i "
                "JOIN pg_class idx ON idx.oid=i.indexrelid JOIN pg_class tbl ON tbl.oid=i.indrelid "
                "JOIN pg_namespace ns ON ns.oid=tbl.relnamespace "
                "WHERE ns.nspname=:schema AND tbl.relname='email_message' AND idx.relname=:name"
            ), {"schema": schema or "public", "name": name}).first()
            if not state or state[0] is not True or state[1] is not True or state[2] is not False or state[3] is not True:
                raise ValueError(f"Email outbox index {name} is invalid or not ready.")
    if not inspector.has_table("email_schema_migration", schema=schema_arg):
        raise ValueError("Email outbox migration history table is missing.")
    ledger_columns = {column["name"] for column in inspector.get_columns("email_schema_migration", schema=schema_arg)}
    ledger_primary_key = tuple(inspector.get_pk_constraint("email_schema_migration", schema=schema_arg).get("constrained_columns") or ())
    if ledger_columns != {"migration_id", "checksum", "applied_at"} or ledger_primary_key != ("migration_id",):
        raise ValueError("Email outbox migration history table is incompatible.")
    ledger_name = "email_schema_migration"
    if schema and connection.dialect.name == "postgresql":
        quoted_schema = connection.dialect.identifier_preparer.quote_schema(schema)
        ledger_name = f"{quoted_schema}.email_schema_migration"
    recorded = connection.execute(text(
        f"SELECT checksum FROM {ledger_name} WHERE migration_id=:migration_id"
    ), {"migration_id": MIGRATION_ID}).scalar_one_or_none()
    if recorded != migration_checksum():
        raise ValueError("Email outbox migration checksum is missing or incompatible.")
    return ["Email outbox columns, indexes, and migration checksum are valid."]
