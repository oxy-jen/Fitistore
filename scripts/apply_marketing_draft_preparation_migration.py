"""Apply only the 20261121 marketing draft-preparation schema migration."""
from __future__ import annotations
import os
import sys
from sqlalchemy import create_engine, inspect, text
from scripts.customer_email_schema_guard import GuardError, _engine_url, required_settings
from app.services.marketing_campaign_preparation_schema import (
    MIGRATION_ID, apply_marketing_campaign_preparation_schema,
    migration_checksum, validate_marketing_campaign_preparation_schema,
)
CONFIRMATION = "APPLY MARKETING DRAFT PREPARATION 20261121"
BACKUP_CONFIRMATION = "I HAVE A VERIFIED RESTORABLE BACKUP"
EXPECTED_COLUMNS = {
    "generation_key": "VARCHAR(160)", "generation_source": "VARCHAR(32)",
    "generation_reason": "VARCHAR(255)", "generation_reference": "TEXT",
}
def main() -> int:
    if os.environ.get("MIGRATION_CONFIRMATION") != CONFIRMATION:
        raise GuardError("Migration confirmation rejected; no database connection was opened.")
    if os.environ.get("BACKUP_CONFIRMATION") != BACKUP_CONFIRMATION:
        raise GuardError("Backup confirmation rejected; no database connection was opened.")
    raw_url, expected_database, expected_schema, _expected_host = required_settings()
    engine = create_engine(_engine_url(raw_url), pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            if connection.dialect.name != "postgresql":
                raise GuardError("Production migration requires PostgreSQL.")
            identity = connection.execute(text(
                "SELECT current_database() AS database_name, current_schema() AS schema_name, current_user AS role_name"
            )).mappings().one()
            if identity["database_name"] != expected_database or identity["schema_name"] != expected_schema:
                raise GuardError("Connected database/schema identity does not match configured production identity.")
            inspector = inspect(connection)
            if not inspector.has_table("email_campaign", schema=expected_schema):
                raise GuardError("Required email_campaign table is missing; no migration was applied.")
            if not inspector.has_table("email_schema_migration", schema=expected_schema):
                raise GuardError("Required email_schema_migration ledger is missing; no migration was applied.")
            if not connection.execute(text("SELECT pg_try_advisory_xact_lock(724930117)")).scalar_one():
                raise GuardError("Another customer/email migration is running; retry later.")
            columns = {row["name"]: row for row in inspector.get_columns("email_campaign", schema=expected_schema)}
            for name, expected_type in EXPECTED_COLUMNS.items():
                if name in columns:
                    actual = columns[name]["type"].compile(dialect=connection.dialect).upper()
                    if actual != expected_type or columns[name]["nullable"] is not True:
                        raise GuardError(f"Existing email_campaign.{name} has an incompatible definition; no migration was applied.")
            changed = apply_marketing_campaign_preparation_schema(connection, "postgresql")
            validate_marketing_campaign_preparation_schema(connection)
            print(
                f"Validated {MIGRATION_ID} on database={identity['database_name']} "
                f"schema={identity['schema_name']} role={identity['role_name']}; "
                f"checksum={migration_checksum()}; newly_applied={str(changed).lower()}."
            )
            print("No other customer/email migration helpers were invoked. No email dispatch was performed.")
        return 0
    finally:
        engine.dispose()
if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GuardError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"FAILED: scoped migration stopped ({type(exc).__name__}); transaction rolled back.", file=sys.stderr)
        raise SystemExit(1)
