"""Additive schema support for email attempts, investigations and worker liveness."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy import text

from app.extensions import db
from app.services.sql_schema_utils import normalize_sql_definition

MIGRATION_ID = "20261116_email_delivery_ops"
_MIGRATION_FILE = Path(__file__).resolve().parents[2] / "migrations" / f"{MIGRATION_ID}.sql"
_TABLE_NAMES = (
    "email_delivery_attempt", "email_delivery_investigation",
    "email_delivery_investigation_event", "email_worker_run",
)


def migration_checksum() -> str:
    return hashlib.sha256(_MIGRATION_FILE.read_bytes()).hexdigest()


def email_delivery_ops_tables():
    from app.models import (
        EmailDeliveryAttempt, EmailDeliveryInvestigation,
        EmailDeliveryInvestigationEvent, EmailWorkerRun,
    )
    return [model.__table__ for model in (
        EmailDeliveryAttempt, EmailDeliveryInvestigation,
        EmailDeliveryInvestigationEvent, EmailWorkerRun,
    )]


def apply_email_delivery_ops_migration(connection, dialect: str) -> bool:
    """Create only four email-operations tables, trigger-protecting the ledger.

    Caller owns one transaction. PostgreSQL callers must hold the customer-email
    advisory transaction lock. No existing table or row is rewritten.
    """
    if dialect not in {"sqlite", "postgresql", "postgres"}:
        raise ValueError("Unsupported email delivery operations migration dialect.")
    prior = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:id"
    ), {"id": MIGRATION_ID}).scalar_one_or_none()
    checksum = migration_checksum()
    if prior is not None and prior != checksum:
        raise ValueError("Email delivery operations migration checksum differs from this code revision.")

    required_parents = {"email_message", "admin_user"}
    from sqlalchemy import inspect
    inspector = inspect(connection)
    missing = required_parents - set(inspector.get_table_names())
    if missing:
        raise ValueError("Required email delivery parent tables are missing: " + ", ".join(sorted(missing)))

    db.metadata.create_all(bind=connection, tables=email_delivery_ops_tables(), checkfirst=True)
    if dialect in {"postgresql", "postgres"}:
        connection.execute(text("""
            CREATE OR REPLACE FUNCTION fity_reject_email_investigation_event_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'email investigation events are append-only'; END;
            $$
        """))
        connection.execute(text("""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_email_investigation_event_immutable'
                  AND tgrelid='email_delivery_investigation_event'::regclass) THEN
                CREATE TRIGGER trg_email_investigation_event_immutable
                BEFORE UPDATE OR DELETE ON email_delivery_investigation_event
                FOR EACH ROW EXECUTE FUNCTION fity_reject_email_investigation_event_mutation();
              END IF;
              IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_email_investigation_event_no_truncate'
                  AND tgrelid='email_delivery_investigation_event'::regclass) THEN
                CREATE TRIGGER trg_email_investigation_event_no_truncate
                BEFORE TRUNCATE ON email_delivery_investigation_event
                FOR EACH STATEMENT EXECUTE FUNCTION fity_reject_email_investigation_event_mutation();
              END IF;
            END $$
        """))
    else:
        connection.execute(text("""
            CREATE TRIGGER IF NOT EXISTS trg_email_investigation_event_immutable
            BEFORE UPDATE ON email_delivery_investigation_event
            BEGIN SELECT RAISE(ABORT, 'email investigation events are append-only'); END
        """))
        connection.execute(text("""
            CREATE TRIGGER IF NOT EXISTS trg_email_investigation_event_no_delete
            BEFORE DELETE ON email_delivery_investigation_event
            BEGIN SELECT RAISE(ABORT, 'email investigation events are append-only'); END
        """))
    connection.execute(text(
        "INSERT INTO email_schema_migration (migration_id, checksum) VALUES (:id,:checksum) "
        "ON CONFLICT (migration_id) DO NOTHING"
    ), {"id": MIGRATION_ID, "checksum": checksum})
    validate_email_delivery_ops_schema(connection)
    from app.services.email_migrations import history_rows, record_migration
    if history_rows(connection):
        record_migration(connection, MIGRATION_ID, state="applied", evidence="delivery operations checksum and schema validator passed")
    return prior is None


def validate_email_delivery_ops_schema(connection, *, schema: str | None = None) -> list[str]:
    from sqlalchemy import inspect
    inspector = inspect(connection)
    schema_arg = schema if connection.dialect.name == "postgresql" else None
    for name in _TABLE_NAMES:
        if not inspector.has_table(name, schema=schema_arg):
            raise ValueError(f"Email delivery operations table {name} is missing.")
    expected = {
        "email_delivery_attempt": {"message_id", "attempt_number", "attempt_key", "provider", "status", "started_at", "completed_at", "provider_message_id", "error_category", "recovery_of_attempt_id"},
        "email_delivery_investigation": {"message_id", "original_attempt_id", "provider", "provider_message_id", "reason", "status", "evidence_summary", "evidence_source", "investigator_admin_id", "started_at", "resolved_at", "resolution_outcome", "resolution_explanation", "recovered_message_id"},
        "email_delivery_investigation_event": {"investigation_id", "actor_admin_id", "event_type", "from_status", "to_status", "evidence_summary", "evidence_source", "explanation", "related_attempt_id", "created_at"},
        "email_worker_run": {"instance_id", "worker_type", "status", "started_at", "last_heartbeat_at", "last_success_at", "last_attempt_at", "current_batch_count", "stopped_at", "last_error_category"},
    }
    for name, columns in expected.items():
        actual = {row["name"] for row in inspector.get_columns(name, schema=schema_arg)}
        if not columns <= actual:
            raise ValueError(f"Email delivery operations table {name} is missing required columns.")
    from app.models import (
        EmailDeliveryAttempt, EmailDeliveryInvestigation,
        EmailDeliveryInvestigationEvent, EmailWorkerRun,
    )
    for model in (EmailDeliveryAttempt, EmailDeliveryInvestigation,
                  EmailDeliveryInvestigationEvent, EmailWorkerRun):
        expected_checks = {
            item.name: normalize_sql_definition(str(item.sqltext))
            for item in model.__table__.constraints
            if item.__class__.__name__ == "CheckConstraint"
        }
        actual_checks = {
            row.get("name"): normalize_sql_definition(row.get("sqltext") or "")
            for row in inspector.get_check_constraints(model.__tablename__, schema=schema_arg)
        }
        for check_name, expected_definition in expected_checks.items():
            if actual_checks.get(check_name) != expected_definition:
                raise ValueError(f"Check constraint {check_name} is missing or has an altered definition.")
    expected_indexes = {
        "email_delivery_attempt": {
            "ix_email_attempt_message_started": (("message_id", "started_at"), False, ""),
        },
        "email_delivery_investigation": {
            "ix_email_delivery_investigation_message_id": (("message_id",), False, ""),
            "uq_email_investigation_open_message": (("message_id",), True, "status<>'resolved'"),
        },
        "email_delivery_investigation_event": {
            "ix_email_delivery_investigation_event_investigation_id": (("investigation_id",), False, ""),
        },
        "email_worker_run": {
            "ix_email_worker_run_last_heartbeat_at": (("last_heartbeat_at",), False, ""),
            "ix_email_worker_run_status_heartbeat": (("status", "last_heartbeat_at"), False, ""),
        },
    }
    for table_name, indexes in expected_indexes.items():
        actual_indexes = {item["name"]: item for item in inspector.get_indexes(table_name, schema=schema_arg)}
        for index_name, (columns, unique, expected_predicate) in indexes.items():
            item = actual_indexes.get(index_name)
            options = (item or {}).get("dialect_options") or {}
            actual_predicate = options.get("postgresql_where")
            if actual_predicate is None:
                actual_predicate = options.get("sqlite_where")
            predicate = normalize_sql_definition(str(actual_predicate) if actual_predicate is not None else "")
            if (not item or tuple(item.get("column_names") or ()) != columns
                    or bool(item.get("unique")) != unique
                    or predicate != normalize_sql_definition(expected_predicate)):
                raise ValueError(f"Email operations index {index_name} is missing or incompatible.")
    for model in (EmailDeliveryAttempt, EmailDeliveryInvestigation,
                  EmailDeliveryInvestigationEvent, EmailWorkerRun):
        table_name = model.__tablename__
        expected_fks = set()
        for constraint in model.__table__.foreign_key_constraints:
            expected_fks.add((
                tuple(element.parent.name for element in constraint.elements),
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
                (constraint.ondelete or "").upper(),
            ))
        actual_fks = set()
        for fk in inspector.get_foreign_keys(table_name, schema=schema_arg):
            actual_fks.add((tuple(fk.get("constrained_columns") or ()),
                            fk.get("referred_table"), tuple(fk.get("referred_columns") or ()),
                            ((fk.get("options") or {}).get("ondelete") or "").upper()))
        if expected_fks != actual_fks:
            raise ValueError(f"Email operations table {table_name} has incompatible foreign keys.")
        expected_unique = {
            tuple(item.columns.keys()) for item in model.__table__.constraints
            if item.__class__.__name__ == "UniqueConstraint"
        }
        actual_unique = {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(table_name, schema=schema_arg)
        }
        actual_unique.update(
            tuple(item.get("column_names") or ())
            for item in inspector.get_indexes(table_name, schema=schema_arg) if item.get("unique")
        )
        if not expected_unique <= actual_unique:
            raise ValueError(f"Email operations table {table_name} has incompatible unique constraints.")
    if connection.dialect.name == "postgresql":
        rows = connection.execute(text("""
            SELECT tgname, tgenabled, (tgtype & 1) <> 0 AS row_level,
                   (tgtype & 2) <> 0 AS before_timing,
                   (tgtype & 16) <> 0 AS on_update, (tgtype & 8) <> 0 AS on_delete,
                   (tgtype & 32) <> 0 AS on_truncate, tgtype, p.proname, p.prosrc,
                   p.prorettype::regtype::text AS return_type, c.relname, n.nspname
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_proc p ON p.oid=t.tgfoid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relname='email_delivery_investigation_event' AND n.nspname=:schema_name AND NOT t.tgisinternal
        """), {"schema_name": schema or connection.execute(text("SELECT current_schema()")).scalar_one()}).mappings().all()
        by_name = {row["tgname"]: row for row in rows}
        for name in ("trg_email_investigation_event_immutable", "trg_email_investigation_event_no_truncate"):
            row = by_name.get(name)
            expected_tgtype = 27 if name == "trg_email_investigation_event_immutable" else 34
            expected_source = "beginraiseexception'emailinvestigationeventsareappend-only';end;"
            if (not row or row["tgenabled"] != "O"
                    or row["proname"] != "fity_reject_email_investigation_event_mutation"
                    or row["return_type"] != "trigger"
                    or row["relname"] != "email_delivery_investigation_event"
                    or row["nspname"] != (schema or connection.execute(text("SELECT current_schema()")).scalar_one())
                    or row["tgtype"] != expected_tgtype
                    or normalize_sql_definition(row["prosrc"]) != expected_source):
                raise ValueError(f"Append-only trigger {name} is missing, disabled, or miswired.")
        mutation = by_name["trg_email_investigation_event_immutable"]
        truncate = by_name["trg_email_investigation_event_no_truncate"]
        if not (mutation["row_level"] and mutation["before_timing"] and mutation["on_update"] and mutation["on_delete"] and not mutation["on_truncate"]):
            raise ValueError("Investigation event mutation trigger operations are incomplete.")
        if not (not truncate["row_level"] and truncate["before_timing"] and truncate["on_truncate"] and not truncate["on_update"] and not truncate["on_delete"]):
            raise ValueError("Investigation event truncate trigger is incomplete.")
        for table_name in _TABLE_NAMES:
            id_column = {item["name"]: item for item in inspector.get_columns(table_name)}.get("id")
            qualified_table = f"{schema}.{table_name}" if schema else table_name
            sequence_name = connection.execute(text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
                                                {"table_name": qualified_table}).scalar_one_or_none()
            default_ref = connection.execute(text("""
                SELECT d.oid AS default_oid, pg_get_expr(d.adbin, d.adrelid) AS expression,
                       EXISTS (
                           SELECT 1 FROM pg_depend dep
                           JOIN pg_class seq ON seq.oid=dep.refobjid
                           WHERE dep.classid='pg_attrdef'::regclass
                             AND dep.objid=d.oid
                             AND dep.refclassid='pg_class'::regclass
                             AND dep.deptype IN ('n','a')
                             AND seq.oid=to_regclass(:sequence_name)
                       ) AS uses_owned_sequence
                FROM pg_attrdef d
                JOIN pg_class tbl ON tbl.oid=d.adrelid
                JOIN pg_namespace ns ON ns.oid=tbl.relnamespace
                JOIN pg_attribute col ON col.attrelid=tbl.oid AND col.attnum=d.adnum
                WHERE ns.nspname=:schema_name AND tbl.relname=:table_name
                  AND col.attname='id'
            """), {"sequence_name": sequence_name or "", "schema_name": schema or connection.execute(
                text("SELECT current_schema()")).scalar_one(), "table_name": table_name}).mappings().first()
            expression = normalize_sql_definition(str((default_ref or {}).get("expression") or ""))
            if (not id_column or not sequence_name or not default_ref
                    or not expression.startswith("nextval(")
                    or not default_ref["uses_owned_sequence"]):
                raise ValueError(f"{table_name}.id is missing its PostgreSQL sequence default.")
        catalog_indexes = connection.execute(text("""
            SELECT idx.relname, i.indisvalid, i.indisready
            FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid
            JOIN pg_class tbl ON tbl.oid=i.indrelid
            JOIN pg_namespace n ON n.oid=tbl.relnamespace
            WHERE n.nspname=:schema_name AND tbl.relname IN ('email_delivery_attempt','email_delivery_investigation',
              'email_delivery_investigation_event','email_worker_run')
        """), {"schema_name": schema or connection.execute(text("SELECT current_schema()")).scalar_one()}).mappings().all()
        index_states = {row["relname"]: row for row in catalog_indexes}
        for index_names in expected_indexes.values():
            for index_name in index_names:
                state = index_states.get(index_name)
                if not state or not state["indisvalid"] or not state["indisready"]:
                    raise ValueError(f"Email operations index {index_name} is invalid or not ready.")
    else:
        trigger_definitions = {row["name"]: normalize_sql_definition(row["sql"] or "")
                               for row in connection.execute(text(
                                   "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
                               )).mappings()}
        expected_trigger_definitions = {
            "trg_email_investigation_event_immutable": normalize_sql_definition(
                "CREATE TRIGGER trg_email_investigation_event_immutable BEFORE UPDATE ON "
                "email_delivery_investigation_event BEGIN SELECT RAISE(ABORT, "
                "'email investigation events are append-only'); END"),
            "trg_email_investigation_event_no_delete": normalize_sql_definition(
                "CREATE TRIGGER trg_email_investigation_event_no_delete BEFORE DELETE ON "
                "email_delivery_investigation_event BEGIN SELECT RAISE(ABORT, "
                "'email investigation events are append-only'); END"),
        }
        for trigger_name, expected_definition in expected_trigger_definitions.items():
            if trigger_definitions.get(trigger_name) != expected_definition:
                raise ValueError(f"SQLite append-only trigger {trigger_name} is missing or altered.")
    if schema and connection.dialect.name == "postgresql":
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("Email delivery operations schema name is invalid.")
        ledger_table = f'{connection.dialect.identifier_preparer.quote(schema)}."email_schema_migration"'
    else:
        ledger_table = "email_schema_migration"
    recorded = connection.execute(text(
        f"SELECT checksum FROM {ledger_table} WHERE migration_id=:id"
    ), {"id": MIGRATION_ID}).scalar_one_or_none()
    if recorded != migration_checksum():
        raise ValueError("Email delivery operations migration checksum is missing or incompatible.")
    return ["Email attempts, investigation ledger and worker liveness schema are valid."]
