"""Append-only database protection for customer consent history."""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import inspect, text

MIGRATION_ID = "20261117_customer_consent_audit_immutable"
_MIGRATION_FILE = Path(__file__).resolve().parents[2] / "migrations" / f"{MIGRATION_ID}.sql"


def migration_checksum() -> str:
    return hashlib.sha256(_MIGRATION_FILE.read_bytes()).hexdigest()


def validate_consent_audit_schema(connection, *, schema: str | None = None) -> None:
    inspector = inspect(connection)
    schema_arg = schema if connection.dialect.name == "postgresql" else None
    if not inspector.has_table("customer_consent_event", schema=schema_arg):
        raise ValueError("Customer consent event table is missing.")
    columns = {item["name"] for item in inspector.get_columns("customer_consent_event", schema=schema_arg)}
    if not {"id", "account_id", "event", "method", "created_at"} <= columns:
        raise ValueError("Customer consent event table is incomplete.")
    ledger = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:migration_id"
    ), {"migration_id": MIGRATION_ID}).scalar_one_or_none()
    if ledger != migration_checksum():
        raise ValueError("Customer consent audit migration checksum is missing or incompatible.")
    if connection.dialect.name == "postgresql":
        schema_name = schema or connection.execute(text("SELECT current_schema()")).scalar_one()
        rows = connection.execute(text("""
            SELECT t.tgname, t.tgenabled, t.tgtype, p.proname, p.prosrc,
                   p.prorettype::regtype::text AS return_type, n.nspname
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relname='customer_consent_event' AND n.nspname=:schema_name AND NOT t.tgisinternal
        """), {"schema_name": schema_name}).mappings().all()
        actual = {row["tgname"]: row for row in rows}
        expected = {
            "trg_customer_consent_event_immutable": 27,
            "trg_customer_consent_event_no_truncate": 34,
        }
        for name, event_mask in expected.items():
            row = actual.get(name)
            source = "".join(str(row["prosrc"]).lower().split()) if row else ""
            if (not row or row["tgenabled"] != "O" or row["tgtype"] != event_mask
                    or row["proname"] != "fity_reject_customer_consent_event_mutation"
                    or row["return_type"] != "trigger" or row["nspname"] != schema_name
                    or source != "beginraiseexception'customerconsenteventsareappend-only';end;"):
                raise ValueError(f"Customer consent append-only trigger {name} is missing or incompatible.")
    elif connection.dialect.name == "sqlite":
        rows = connection.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='customer_consent_event'"
        )).mappings().all()
        actual = {row["name"]: "".join((row["sql"] or "").lower().split()) for row in rows}
        for name, operation in (("trg_customer_consent_event_immutable_update", "beforeupdate"),
                                ("trg_customer_consent_event_immutable_delete", "beforedelete")):
            definition = actual.get(name, "")
            if operation not in definition or "raise(abort,'customerconsenteventsareappend-only')" not in definition:
                raise ValueError(f"Customer consent append-only trigger {name} is missing or incompatible.")
    else:
        raise ValueError("Unsupported customer consent audit schema dialect.")


def apply_consent_audit_migration(connection, dialect: str) -> bool:
    """Create only mutation-blocking triggers; caller owns the transaction/lock."""
    if dialect not in {"sqlite", "postgresql", "postgres"}:
        raise ValueError("Unsupported customer consent audit migration dialect.")
    inspector = inspect(connection)
    if not inspector.has_table("customer_consent_event"):
        raise ValueError("Customer consent event table must exist before this migration.")
    if not inspector.has_table("email_schema_migration"):
        raise ValueError("The email schema migration ledger must exist before this migration.")
    checksum = migration_checksum()
    prior = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:migration_id"
    ), {"migration_id": MIGRATION_ID}).scalar_one_or_none()
    if prior is not None and prior != checksum:
        raise ValueError("Recorded customer consent audit migration checksum differs from this code revision.")
    if prior is None:
        if dialect in {"postgresql", "postgres"}:
            connection.execute(text("""
                CREATE OR REPLACE FUNCTION fity_reject_customer_consent_event_mutation()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'customer consent events are append-only'; END;
                $$
            """))
            connection.execute(text("""
                DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_customer_consent_event_immutable'
                      AND tgrelid='customer_consent_event'::regclass) THEN
                    CREATE TRIGGER trg_customer_consent_event_immutable
                    BEFORE UPDATE OR DELETE ON customer_consent_event
                    FOR EACH ROW EXECUTE FUNCTION fity_reject_customer_consent_event_mutation();
                  END IF;
                  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_customer_consent_event_no_truncate'
                      AND tgrelid='customer_consent_event'::regclass) THEN
                    CREATE TRIGGER trg_customer_consent_event_no_truncate
                    BEFORE TRUNCATE ON customer_consent_event
                    FOR EACH STATEMENT EXECUTE FUNCTION fity_reject_customer_consent_event_mutation();
                  END IF;
                END $$
            """))
        else:
            connection.execute(text("""
                CREATE TRIGGER IF NOT EXISTS trg_customer_consent_event_immutable_update
                BEFORE UPDATE ON customer_consent_event
                BEGIN SELECT RAISE(ABORT, 'customer consent events are append-only'); END
            """))
            connection.execute(text("""
                CREATE TRIGGER IF NOT EXISTS trg_customer_consent_event_immutable_delete
                BEFORE DELETE ON customer_consent_event
                BEGIN SELECT RAISE(ABORT, 'customer consent events are append-only'); END
            """))
        connection.execute(text(
            "INSERT INTO email_schema_migration (migration_id, checksum) VALUES (:migration_id, :checksum)"
        ), {"migration_id": MIGRATION_ID, "checksum": checksum})
    validate_consent_audit_schema(connection)
    from app.services.email_migrations import history_rows, record_migration
    if history_rows(connection):
        record_migration(connection, MIGRATION_ID, state="applied", evidence="customer consent append-only triggers validated")
    return prior is None
