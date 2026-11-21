"""Additive schema support for generated marketing campaign drafts."""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import inspect, text

MIGRATION_ID = "20261121_marketing_campaign_draft_preparation"
_MIGRATION_FILE = Path(__file__).resolve().parents[2] / "migrations" / f"{MIGRATION_ID}.sql"
_COLUMNS = {
    "generation_key": ("VARCHAR(160)", True),
    "generation_source": ("VARCHAR(32)", True),
    "generation_reason": ("VARCHAR(255)", True),
    "generation_reference": ("TEXT", True),
}


def migration_checksum() -> str:
    return hashlib.sha256(_MIGRATION_FILE.read_bytes()).hexdigest()


def _normalise(value: str | None) -> str:
    return "".join(str(value or "").lower().replace('"', "").replace("(", "").replace(")", "").split())


def validate_marketing_campaign_preparation_schema(connection) -> None:
    inspector = inspect(connection)
    columns = {row["name"]: row for row in inspector.get_columns("email_campaign")}
    expected_types = {"generation_key": "VARCHAR(160)", "generation_source": "VARCHAR(32)",
                      "generation_reason": "VARCHAR(255)", "generation_reference": "TEXT"}
    for name, expected in expected_types.items():
        column = columns.get(name)
        if not column:
            raise ValueError(f"Marketing draft preparation column email_campaign.{name} is missing.")
        actual = column["type"].compile(dialect=connection.dialect).upper()
        if actual != expected or bool(column["nullable"]) is not True:
            raise ValueError(f"Marketing draft preparation column email_campaign.{name} is incompatible.")

    indexes = {row["name"]: row for row in inspector.get_indexes("email_campaign")}
    index = indexes.get("uq_email_campaign_generation_key")
    predicate = ""
    if index:
        options = index.get("dialect_options", {})
        predicate_value = options.get("sqlite_where")
        if predicate_value is None:
            predicate_value = options.get("postgresql_where")
        predicate = "" if predicate_value is None else str(predicate_value)
    if (not index or tuple(index.get("column_names") or ()) != ("generation_key",)
            or not index.get("unique") or _normalise(predicate) != "generation_keyisnotnull"):
        # Some SQLAlchemy versions expose a partial-index predicate only in
        # dialect_options; validate PostgreSQL catalogs below as the authority.
        if connection.dialect.name != "postgresql":
            raise ValueError("Marketing draft preparation unique index is missing or incompatible.")
    if connection.dialect.name == "postgresql":
        row = connection.execute(text("""
            SELECT i.indisunique, i.indisvalid, i.indisready,
                   pg_get_expr(i.indpred, i.indrelid) AS predicate,
                   pg_get_indexdef(i.indexrelid) AS definition
              FROM pg_index i
              JOIN pg_class idx ON idx.oid=i.indexrelid
              JOIN pg_class tbl ON tbl.oid=i.indrelid
              JOIN pg_namespace ns ON ns.oid=tbl.relnamespace
             WHERE ns.nspname=current_schema() AND tbl.relname='email_campaign'
               AND idx.relname='uq_email_campaign_generation_key'
        """)).mappings().first()
        if (not row or not row["indisunique"] or not row["indisvalid"] or not row["indisready"]
                or _normalise(row["predicate"]) != "generation_keyisnotnull"
                or "generation_key" not in _normalise(row["definition"])):
            raise ValueError("Marketing draft preparation PostgreSQL unique index is invalid or incompatible.")

    ledger = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:id"
    ), {"id": MIGRATION_ID}).scalar_one_or_none()
    if ledger != migration_checksum():
        raise ValueError("Marketing draft preparation migration checksum is missing or incompatible.")


def apply_marketing_campaign_preparation_schema(connection, dialect: str) -> bool:
    if dialect not in {"sqlite", "postgresql", "postgres"}:
        raise ValueError("Unsupported marketing draft preparation migration dialect.")
    if not inspect(connection).has_table("email_campaign"):
        raise ValueError("Email campaign table must exist before draft preparation schema setup.")
    checksum = migration_checksum()
    prior = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:id"
    ), {"id": MIGRATION_ID}).scalar_one_or_none()
    if prior is not None and prior != checksum:
        raise ValueError("Marketing draft preparation migration checksum differs from this code revision.")

    columns = {row["name"] for row in inspect(connection).get_columns("email_campaign")}
    for name, (sql_type, nullable) in _COLUMNS.items():
        if name not in columns:
            connection.execute(text(
                f"ALTER TABLE email_campaign ADD COLUMN {name} {sql_type}{'' if nullable else ' NOT NULL'}"
            ))
    connection.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_email_campaign_generation_key "
        "ON email_campaign (generation_key) WHERE generation_key IS NOT NULL"
    ))
    connection.execute(text(
        "INSERT INTO email_schema_migration (migration_id, checksum) VALUES (:id,:checksum) "
        "ON CONFLICT (migration_id) DO NOTHING"
    ), {"id": MIGRATION_ID, "checksum": checksum})
    validate_marketing_campaign_preparation_schema(connection)
    if inspect(connection).has_table("email_migration_history"):
        from app.services.email_migrations import record_migration
        record_migration(connection, MIGRATION_ID, state="applied",
                         evidence="marketing campaign draft preparation schema validated")
    return prior is None
