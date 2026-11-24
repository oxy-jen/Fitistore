"""Transactional marketing suppression lifecycle and PostgreSQL schema migration."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import inspect, select, text

from app.extensions import db
from app.models import (
    AdminUser, CustomerAccount, CustomerConsentEvent, CustomerToken,
    MarketingSuppressionCase, MarketingSuppressionEvent,
)
from app.services.sql_schema_utils import normalize_sql_definition as normalized_definition

MIGRATION_ID = "20261113_marketing_suppression"
MIGRATION_LOCK_ID = 724930117

# Each tuple element is one complete SQL statement. In particular, the trigger
# function body is deliberately passed intact; no semicolon-based splitting.
SUPPRESSION_DDL = (
    """CREATE TABLE IF NOT EXISTS marketing_suppression_schema_migration (
        migration_id VARCHAR(120) PRIMARY KEY,
        checksum VARCHAR(64) NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS marketing_suppression_case (
        id BIGSERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES customer_account(id) ON DELETE RESTRICT,
        source VARCHAR(24) NOT NULL,
        reason_code VARCHAR(48) NOT NULL,
        detected_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        provider_name VARCHAR(48),
        metadata JSONB,
        cleared_at TIMESTAMP WITHOUT TIME ZONE,
        cleared_by_admin_id INTEGER REFERENCES admin_user(id) ON DELETE RESTRICT,
        clearance_reason TEXT,
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_marketing_suppression_case_id_account UNIQUE (id, account_id),
        CONSTRAINT ck_marketing_suppression_case_source CHECK (source IN ('admin', 'provider', 'legacy_unknown')),
        CONSTRAINT ck_marketing_suppression_case_reason CHECK (reason_code IN ('admin_review', 'hard_bounce', 'spam_complaint', 'invalid_address', 'provider_unsubscribe_unclassified', 'legacy_unknown')),
        CONSTRAINT ck_marketing_suppression_case_provider CHECK (source <> 'provider' OR provider_name IS NOT NULL),
        CONSTRAINT ck_marketing_suppression_case_source_reason CHECK ((source = 'admin' AND reason_code = 'admin_review') OR (source = 'provider' AND reason_code IN ('hard_bounce', 'spam_complaint', 'invalid_address', 'provider_unsubscribe_unclassified')) OR (source = 'legacy_unknown' AND reason_code = 'legacy_unknown')),
        CONSTRAINT ck_marketing_suppression_case_clearance CHECK ((cleared_at IS NULL AND cleared_by_admin_id IS NULL AND clearance_reason IS NULL) OR (cleared_at IS NOT NULL AND cleared_by_admin_id IS NOT NULL AND clearance_reason IS NOT NULL AND length(trim(clearance_reason)) > 0))
    )""",
    """CREATE TABLE IF NOT EXISTS marketing_suppression_event (
        id BIGSERIAL PRIMARY KEY,
        suppression_case_id BIGINT NOT NULL,
        account_id INTEGER NOT NULL REFERENCES customer_account(id) ON DELETE RESTRICT,
        event_type VARCHAR(32) NOT NULL,
        actor_type VARCHAR(24) NOT NULL,
        admin_id INTEGER REFERENCES admin_user(id) ON DELETE RESTRICT,
        source VARCHAR(24) NOT NULL,
        reason_code VARCHAR(48) NOT NULL,
        reason_text TEXT,
        occurred_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        provider_name VARCHAR(48),
        provider_event_id VARCHAR(255),
        provider_message_id VARCHAR(255),
        dedupe_fingerprint VARCHAR(64),
        legacy_consent_event_id INTEGER REFERENCES customer_consent_event(id) ON DELETE RESTRICT,
        idempotency_key VARCHAR(255),
        metadata JSONB,
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_marketing_suppression_event_case_account FOREIGN KEY (suppression_case_id, account_id) REFERENCES marketing_suppression_case(id, account_id) ON DELETE RESTRICT,
        CONSTRAINT uq_marketing_suppression_event_idempotency_key UNIQUE (idempotency_key),
        CONSTRAINT ck_marketing_suppression_event_type CHECK (event_type IN ('suppression_detected', 'suppression_observed', 'review_started', 'suppression_cleared', 'legacy_imported')),
        CONSTRAINT ck_marketing_suppression_event_actor CHECK (actor_type IN ('admin', 'provider', 'system', 'legacy')),
        CONSTRAINT ck_marketing_suppression_event_admin_actor CHECK ((actor_type = 'admin' AND admin_id IS NOT NULL) OR (actor_type <> 'admin' AND admin_id IS NULL)),
        CONSTRAINT ck_marketing_suppression_event_source CHECK (source IN ('admin', 'provider', 'legacy_unknown')),
        CONSTRAINT ck_marketing_suppression_event_reason CHECK (reason_code IN ('admin_review', 'hard_bounce', 'spam_complaint', 'invalid_address', 'provider_unsubscribe_unclassified', 'legacy_unknown')),
        CONSTRAINT ck_marketing_suppression_event_source_actor_reason CHECK ((source = 'admin' AND actor_type = 'admin' AND reason_code = 'admin_review') OR (source = 'provider' AND actor_type IN ('provider', 'admin') AND provider_name IS NOT NULL AND reason_code IN ('hard_bounce', 'spam_complaint', 'invalid_address', 'provider_unsubscribe_unclassified')) OR (source = 'legacy_unknown' AND actor_type IN ('legacy', 'system', 'admin') AND reason_code = 'legacy_unknown'))
    )""",
    "CREATE INDEX IF NOT EXISTS ix_marketing_suppression_case_account_detected ON marketing_suppression_case (account_id, detected_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketing_suppression_case_active ON marketing_suppression_case (account_id, source, reason_code) WHERE cleared_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_marketing_suppression_event_account_occurred ON marketing_suppression_event (account_id, occurred_at)",
    "CREATE INDEX IF NOT EXISTS ix_marketing_suppression_event_case_occurred ON marketing_suppression_event (suppression_case_id, occurred_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_marketing_suppression_event_provider_id ON marketing_suppression_event (provider_name, provider_event_id) WHERE provider_event_id IS NOT NULL",
    """CREATE OR REPLACE FUNCTION fity_reject_suppression_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'marketing suppression events are append-only';
        END; $$""",
    """CREATE TRIGGER trg_marketing_suppression_event_immutable
        BEFORE UPDATE OR DELETE ON marketing_suppression_event
        FOR EACH ROW EXECUTE FUNCTION fity_reject_suppression_event_mutation()""",
    """CREATE TRIGGER trg_marketing_suppression_event_no_truncate
        BEFORE TRUNCATE ON marketing_suppression_event
        FOR EACH STATEMENT EXECUTE FUNCTION fity_reject_suppression_event_mutation()""",
)

LEGACY_IMPORT_DDL = (
    """INSERT INTO marketing_suppression_case
       (account_id, source, reason_code, detected_at, metadata)
       SELECT account.id, 'legacy_unknown', 'legacy_unknown',
              COALESCE(MIN(consent.created_at), account.updated_at, CURRENT_TIMESTAMP),
              jsonb_build_object('migration', 'legacy_suppression_import')
       FROM customer_account AS account
       LEFT JOIN customer_consent_event AS consent
         ON consent.account_id = account.id
        AND (consent.event = 'suppressed' OR (consent.event = 'unsubscribed' AND consent.method = 'brevo_webhook'))
       WHERE account.marketing_status = 'suppressed' OR consent.id IS NOT NULL
       GROUP BY account.id, account.updated_at
       ON CONFLICT (account_id, source, reason_code) WHERE cleared_at IS NULL DO NOTHING""",
    """INSERT INTO marketing_suppression_event
       (suppression_case_id, account_id, event_type, actor_type, source, reason_code,
        reason_text, occurred_at, legacy_consent_event_id, idempotency_key, metadata)
       SELECT cases.id, cases.account_id, 'legacy_imported', 'legacy', 'legacy_unknown',
              'legacy_unknown', 'Imported from historical generic suppression consent event.',
              consent.created_at, consent.id, 'legacy-consent-event:' || consent.id,
              jsonb_build_object('legacy_method', consent.method)
       FROM customer_consent_event AS consent
       JOIN marketing_suppression_case AS cases
         ON cases.account_id = consent.account_id
        AND cases.source = 'legacy_unknown' AND cases.reason_code = 'legacy_unknown'
        AND cases.cleared_at IS NULL
       WHERE consent.event = 'suppressed'
          OR (consent.event = 'unsubscribed' AND consent.method = 'brevo_webhook')
       ON CONFLICT (idempotency_key) DO NOTHING""",
    """INSERT INTO marketing_suppression_event
       (suppression_case_id, account_id, event_type, actor_type, source, reason_code,
        reason_text, idempotency_key, metadata)
       SELECT cases.id, account.id, 'legacy_imported', 'legacy', 'legacy_unknown',
              'legacy_unknown', 'Imported from legacy suppressed account status.',
              'legacy-suppressed-account:' || account.id,
              jsonb_build_object('migration', 'legacy_suppression_import')
       FROM customer_account AS account
       JOIN marketing_suppression_case AS cases
         ON cases.account_id = account.id
        AND cases.source = 'legacy_unknown' AND cases.reason_code = 'legacy_unknown'
        AND cases.cleared_at IS NULL
       WHERE account.marketing_status = 'suppressed'
         AND NOT EXISTS (SELECT 1 FROM customer_consent_event AS consent
                         WHERE consent.account_id = account.id
                           AND (consent.event = 'suppressed' OR (consent.event = 'unsubscribed' AND consent.method = 'brevo_webhook')))
       ON CONFLICT (idempotency_key) DO NOTHING""",
)


def migration_checksum() -> str:
    payload = "\n-- statement boundary --\n".join((*SUPPRESSION_DDL, *LEGACY_IMPORT_DDL))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_check_definitions() -> dict[str, str]:
    checks = {}
    for statement in SUPPRESSION_DDL:
        for match in re.finditer(r"CONSTRAINT\s+(ck_[a-z0-9_]+)\s+CHECK\s*\(", statement, re.I):
            opening = match.end() - 1
            depth, quoted, closing = 0, False, None
            index = opening
            while index < len(statement):
                char = statement[index]
                if char == "'":
                    if quoted and index + 1 < len(statement) and statement[index + 1] == "'":
                        index += 1
                    else:
                        quoted = not quoted
                elif not quoted and char == "(":
                    depth += 1
                elif not quoted and char == ")":
                    depth -= 1
                    if depth == 0:
                        closing = index
                        break
                index += 1
            if closing is None:
                raise ValueError("Malformed CHECK definition in suppression migration DDL.")
            checks[match.group(1)] = normalized_definition("CHECK " + statement[opening:closing + 1])
    return checks


def _verify_catalog_checks(actual_checks: dict[str, str], validated: dict[str, bool]) -> None:
    expected = _expected_check_definitions()
    normalized = {name: normalized_definition(value) for name, value in actual_checks.items()}
    if (normalized != expected or set(validated) != set(expected)
            or not all(validated.values())):
        raise ValueError("Missing or altered/unvalidated marketing suppression CHECK constraint.")


def _verify_catalog_indexes(rows, expected_indexes, *, schema: str) -> None:
    by_name = {row["index_name"]: row for row in rows}
    for name, (table, columns, unique, predicate) in expected_indexes.items():
        row = by_name.get(name)
        if not row or not row["indisvalid"] or not row["indisready"] or bool(row["indisunique"]) != unique:
            raise ValueError(f"Missing or invalid marketing suppression index: {name}.")
        definition = str(row["definition"] or "")
        definition = re.sub(rf"\bON\s+{re.escape(schema)}\.", "ON ", definition, flags=re.I)
        index_type = "UNIQUE " if unique else ""
        predicate_sql = None
        if predicate:
            predicate_sql = re.sub(r"isnotnull$", " IS NOT NULL", predicate)
            predicate_sql = re.sub(r"isnull$", " IS NULL", predicate_sql)
        expected = normalized_definition(
            f"CREATE {index_type}INDEX {name} ON {table} USING btree ({', '.join(columns)})"
            + (f" WHERE ({predicate_sql})" if predicate_sql else "")
        )
        actual = normalized_definition(definition)
        if (actual != expected or normalized_definition(row["predicate"]) != normalized_definition(predicate)
                or row["access_method"] != "btree" or row["has_expressions"]):
            raise ValueError(f"Incompatible marketing suppression index definition: {name}.")


def _verify_catalog_triggers(rows, *, schema: str) -> None:
    triggers = {row["trigger_name"]: row for row in rows}
    expected_types = {
        "trg_marketing_suppression_event_immutable": 27,
        "trg_marketing_suppression_event_no_truncate": 34,
    }
    if set(triggers) != set(expected_types):
        raise ValueError("Missing or unexpected append-only suppression ledger trigger.")
    expected_function = normalized_definition("BEGIN RAISE EXCEPTION 'marketing suppression events are append-only'; END;")
    for name, row in triggers.items():
        if (row["tgenabled"] != "O" or int(row["trigger_type"]) != expected_types[name]
                or row["function_name"] != "fity_reject_suppression_event_mutation"
                or row["function_schema"] != schema or row["function_return"] != "trigger"
                or row["function_language"] != "plpgsql" or row["is_constraint_trigger"]
                or row["is_internal"] or row["function_arg_count"] != 0 or row["trigger_arg_count"] != 0
                or normalized_definition(row["function_source"]) != expected_function):
            raise ValueError("Suppression ledger trigger/function definition is disabled or incompatible.")


def _verify_sequence_default(sequence: str | None, default: str | None, table_name: str) -> None:
    if not sequence or not default:
        raise ValueError(f"Missing sequence-backed id default for {table_name}.")
    sequence_path = str(sequence).replace('"', "").lower()
    sequence_name = sequence_path.split(".")[-1]
    actual = normalized_definition(default)
    allowed = {
        normalized_definition(f"nextval('{sequence_path}'::regclass)"),
        normalized_definition(f"nextval('{sequence_name}'::regclass)"),
    }
    if actual not in allowed:
        raise ValueError(f"ID default for {table_name} points to a different sequence.")


def validate_marketing_suppression_schema(connection, *, schema: str = "public") -> None:
    """Fail closed unless tables, constraints, partial indexes and triggers match."""
    if connection.dialect.name != "postgresql":
        raise ValueError("Marketing suppression schema validation requires PostgreSQL.")
    inspector = inspect(connection)
    required_tables = {
        "marketing_suppression_case": MarketingSuppressionCase,
        "marketing_suppression_event": MarketingSuppressionEvent,
        "marketing_suppression_schema_migration": None,
    }
    tables = set(inspector.get_table_names(schema=schema))
    if missing := set(required_tables) - tables:
        raise ValueError("Missing marketing suppression table(s): " + ", ".join(sorted(missing)))
    for table_name, model in required_tables.items():
        actual_columns = {row["name"]: row for row in inspector.get_columns(table_name, schema=schema)}
        if model is None:
            expected = {"migration_id", "checksum", "applied_at"}
            if set(actual_columns) != expected:
                raise ValueError("Incompatible marketing suppression migration ledger columns.")
            if (actual_columns["migration_id"]["nullable"]
                    or actual_columns["checksum"]["nullable"]
                    or actual_columns["applied_at"]["nullable"]
                    or "VARCHAR(120)" not in actual_columns["migration_id"]["type"].compile(dialect=connection.dialect).upper()
                    or "VARCHAR(64)" not in actual_columns["checksum"]["type"].compile(dialect=connection.dialect).upper()
                    or "TIMESTAMP WITH TIME ZONE" not in actual_columns["applied_at"]["type"].compile(dialect=connection.dialect).upper()
                    or normalized_definition(actual_columns["applied_at"].get("default")) not in {"current_timestamp", "now"}):
                raise ValueError("Incompatible type, nullability, or default in marketing suppression migration ledger.")
            if tuple(inspector.get_pk_constraint(table_name, schema=schema).get("constrained_columns") or ()) != ("migration_id",):
                raise ValueError("Incompatible migration ledger primary key.")
            continue
        expected_columns = set(model.__table__.columns.keys())
        if set(actual_columns) != expected_columns:
            raise ValueError(f"Missing or unexpected columns in {table_name}.")
        for column in model.__table__.columns:
            actual = actual_columns[column.name]
            if bool(actual["nullable"]) != bool(column.nullable):
                raise ValueError(f"Incompatible nullability for {table_name}.{column.name}.")
            expected_type = column.type.compile(dialect=connection.dialect).upper()
            actual_type = actual["type"].compile(dialect=connection.dialect).upper()
            if expected_type != actual_type:
                raise ValueError(f"Incompatible type for {table_name}.{column.name}.")
            if column.name in {"created_at", "updated_at", "detected_at", "occurred_at"}:
                default = normalized_definition(actual.get("default"))
                if default not in {"current_timestamp", "now"}:
                    raise ValueError(f"Missing timestamp default for {table_name}.{column.name}.")
        sequence = connection.execute(
            text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
            {"table_name": f"{schema}.{table_name}"},
        ).scalar_one_or_none()
        if not sequence:
            raise ValueError(f"Missing generated identifier sequence for {table_name}.")
    check_rows = connection.execute(text("""
        SELECT constraint_row.conname, pg_get_constraintdef(constraint_row.oid, true) AS definition,
               constraint_row.convalidated
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS table_class ON table_class.oid = constraint_row.conrelid
        JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
        WHERE namespace.nspname = :schema
          AND table_class.relname IN ('marketing_suppression_case', 'marketing_suppression_event')
          AND constraint_row.contype = 'c'
    """), {"schema": schema}).mappings().all()
    if len(check_rows) != len(_expected_check_definitions()):
        raise ValueError("Missing or unexpected marketing suppression CHECK constraint.")
    _verify_catalog_checks(
        {row["conname"]: row["definition"] for row in check_rows},
        {row["conname"]: row["convalidated"] for row in check_rows},
    )
    expected_indexes = {
        "ix_marketing_suppression_case_account_detected": ("marketing_suppression_case", ("account_id", "detected_at"), False, None),
        "uq_marketing_suppression_case_active": ("marketing_suppression_case", ("account_id", "source", "reason_code"), True, "cleared_at IS NULL"),
        "ix_marketing_suppression_event_account_occurred": ("marketing_suppression_event", ("account_id", "occurred_at"), False, None),
        "ix_marketing_suppression_event_case_occurred": ("marketing_suppression_event", ("suppression_case_id", "occurred_at"), False, None),
        "uq_marketing_suppression_event_provider_id": ("marketing_suppression_event", ("provider_name", "provider_event_id"), True, "provider_event_id IS NOT NULL"),
    }
    actual_indexes = {}
    for table_name in ("marketing_suppression_case", "marketing_suppression_event"):
        for index in inspector.get_indexes(table_name, schema=schema):
            options = index.get("dialect_options") or {}
            actual_indexes[index["name"]] = (
                table_name, tuple(index.get("column_names") or ()), bool(index.get("unique")),
                normalized_definition(options.get("postgresql_where")),
            )
    constraint_index_names = {"uq_marketing_suppression_case_id_account", "uq_marketing_suppression_event_idempotency_key"}
    index_rows = connection.execute(text("""
        SELECT index_class.relname AS index_name, table_class.relname AS table_name,
               index_row.indisunique, index_row.indisvalid, index_row.indisready,
               pg_get_indexdef(index_row.indexrelid) AS definition,
               pg_get_expr(index_row.indpred, index_row.indrelid, true) AS predicate,
               access_method.amname AS access_method, index_row.indexprs IS NOT NULL AS has_expressions
        FROM pg_index AS index_row
        JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid
        JOIN pg_class AS table_class ON table_class.oid = index_row.indrelid
        JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
        JOIN pg_am AS access_method ON access_method.oid = index_class.relam
        WHERE namespace.nspname = :schema
          AND table_class.relname IN ('marketing_suppression_case', 'marketing_suppression_event')
    """), {"schema": schema}).mappings().all()
    if (set(actual_indexes) - constraint_index_names) != set(expected_indexes):
        raise ValueError("Missing or unexpected marketing suppression index.")
    for name, (table, columns, unique, predicate) in expected_indexes.items():
        actual = actual_indexes[name]
        expected_predicate = normalized_definition(predicate)
        if actual != (table, columns, unique, expected_predicate):
            raise ValueError(f"Incompatible marketing suppression index: {name}.")
    _verify_catalog_indexes(index_rows, expected_indexes, schema=schema)
    expected_fks = {
        ("marketing_suppression_case", ("account_id",), "customer_account", ("id",), "RESTRICT"),
        ("marketing_suppression_case", ("cleared_by_admin_id",), "admin_user", ("id",), "RESTRICT"),
        ("marketing_suppression_event", ("account_id",), "customer_account", ("id",), "RESTRICT"),
        ("marketing_suppression_event", ("suppression_case_id", "account_id"), "marketing_suppression_case", ("id", "account_id"), "RESTRICT"),
        ("marketing_suppression_event", ("admin_id",), "admin_user", ("id",), "RESTRICT"),
        ("marketing_suppression_event", ("legacy_consent_event_id",), "customer_consent_event", ("id",), "RESTRICT"),
    }
    actual_fks = set()
    for table_name in ("marketing_suppression_case", "marketing_suppression_event"):
        for fk in inspector.get_foreign_keys(table_name, schema=schema):
            actual_fks.add((table_name, tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]), str((fk.get("options") or {}).get("ondelete", "")).upper()))
    if actual_fks != expected_fks:
        raise ValueError("Missing or incompatible suppression foreign key, including account consistency key.")
    fk_rows = connection.execute(text("""
        SELECT constraint_row.conname, constraint_row.convalidated,
               constraint_row.condeferrable, constraint_row.condeferred,
               constraint_row.confmatchtype, constraint_row.confdeltype, constraint_row.confupdtype,
               pg_get_constraintdef(constraint_row.oid, true) AS definition
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS table_class ON table_class.oid = constraint_row.conrelid
        JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
        WHERE namespace.nspname = :schema
          AND table_class.relname IN ('marketing_suppression_case', 'marketing_suppression_event')
          AND constraint_row.contype = 'f'
    """), {"schema": schema}).mappings().all()
    if (len(fk_rows) != len(expected_fks)
            or any(not row["convalidated"] or row["condeferrable"] or row["condeferred"]
                   or row["confmatchtype"] != "s" or row["confdeltype"] != "r"
                   or row["confupdtype"] != "a" for row in fk_rows)):
        raise ValueError("Suppression foreign keys are unvalidated or have altered match/delete semantics.")
    expected_uniques = {
        ("marketing_suppression_case", "uq_marketing_suppression_case_id_account", ("id", "account_id")),
        ("marketing_suppression_event", "uq_marketing_suppression_event_idempotency_key", ("idempotency_key",)),
    }
    actual_uniques = {
        (table_name, row.get("name") or "", tuple(row.get("column_names") or ()))
        for table_name in ("marketing_suppression_case", "marketing_suppression_event")
        for row in inspector.get_unique_constraints(table_name, schema=schema)
    }
    if actual_uniques != expected_uniques:
        raise ValueError("Missing or incompatible suppression unique constraint.")
    constraint_indexes = {row["index_name"]: row for row in index_rows}
    for name in constraint_index_names:
        row = constraint_indexes.get(name)
        if not row or not row["indisvalid"] or not row["indisready"] or not row["indisunique"]:
            raise ValueError(f"Missing or invalid unique-constraint index: {name}.")
    for table_name, expected_pk in (("marketing_suppression_case", ("id",)), ("marketing_suppression_event", ("id",))):
        if tuple(inspector.get_pk_constraint(table_name, schema=schema).get("constrained_columns") or ()) != expected_pk:
            raise ValueError(f"Incompatible primary key on {table_name}.")
    trigger_rows = connection.execute(text("""
        SELECT trigger.tgname AS trigger_name, trigger.tgenabled,
               pg_get_triggerdef(trigger.oid) AS definition,
               procedure.prosrc AS function_source,
               procedure.proname AS function_name,
               function_namespace.nspname AS function_schema,
               procedure.pronargs AS function_arg_count,
               trigger.tgnargs AS trigger_arg_count,
               procedure.prorettype::regtype::text AS function_return,
               language.lanname AS function_language,
               trigger.tgtype AS trigger_type,
               trigger.tgfoid::regprocedure::text AS function_identity,
               trigger.tgconstraint <> 0 AS is_constraint_trigger,
               trigger.tgisinternal AS is_internal
        FROM pg_trigger AS trigger
        JOIN pg_class AS table_class ON table_class.oid = trigger.tgrelid
        JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
        JOIN pg_proc AS procedure ON procedure.oid = trigger.tgfoid
        JOIN pg_namespace AS function_namespace ON function_namespace.oid = procedure.pronamespace
        JOIN pg_language AS language ON language.oid = procedure.prolang
        WHERE namespace.nspname = :schema
          AND table_class.relname = 'marketing_suppression_event'
          AND NOT trigger.tgisinternal
    """), {"schema": schema}).mappings().all()
    _verify_catalog_triggers(trigger_rows, schema=schema)
    for table_name in ("marketing_suppression_case", "marketing_suppression_event"):
        id_default = connection.execute(text("""
            SELECT pg_get_expr(default_row.adbin, default_row.adrelid, true)
            FROM pg_attrdef AS default_row
            JOIN pg_class AS table_class ON table_class.oid = default_row.adrelid
            JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
            JOIN pg_attribute AS column_row ON column_row.attrelid = table_class.oid
                                            AND column_row.attnum = default_row.adnum
            WHERE namespace.nspname = :schema AND table_class.relname = :table
              AND column_row.attname = 'id'
        """), {"schema": schema, "table": table_name}).scalar_one_or_none()
        sequence = connection.execute(text("SELECT pg_get_serial_sequence(:table_name, 'id')"), {"table_name": f"{schema}.{table_name}"}).scalar_one_or_none()
        _verify_sequence_default(sequence, id_default, table_name)
    ledger = connection.execute(text("SELECT checksum FROM marketing_suppression_schema_migration WHERE migration_id = :id"), {"id": MIGRATION_ID}).scalar_one_or_none()
    if ledger != migration_checksum():
        raise ValueError("Marketing suppression migration identity/checksum is missing or incompatible.")


def apply_marketing_suppression_migration(connection) -> bool:
    """Apply the additive PostgreSQL schema migration in caller's locked transaction."""
    if connection.dialect.name != "postgresql":
        raise ValueError("Marketing suppression migration is PostgreSQL-only.")
    required_parents = {"customer_account", "admin_user", "customer_consent_event"}
    if not required_parents <= set(inspect(connection).get_table_names()):
        raise ValueError("Customer account, administrator, and consent tables must exist before this migration.")
    result = connection.execute(text("""SELECT table_name, column_name, data_type
        FROM information_schema.columns WHERE table_schema = current_schema()
          AND (table_name, column_name) IN (('customer_account','id'), ('admin_user','id'), ('customer_consent_event','id'))""")).all()
    if {(row[0], row[1], row[2]) for row in result} != {
        ("customer_account", "id", "integer"), ("admin_user", "id", "integer"), ("customer_consent_event", "id", "integer")
    }:
        raise ValueError("Parent primary-key types do not match the expected INTEGER schema.")
    checksum = migration_checksum()
    # The tracking relation is the only object created before checking the
    # migration marker; CREATE IF NOT EXISTS is harmless for an identical table.
    connection.exec_driver_sql(SUPPRESSION_DDL[0])
    ledger_columns = {row["name"] for row in inspect(connection).get_columns("marketing_suppression_schema_migration")}
    if ledger_columns != {"migration_id", "checksum", "applied_at"}:
        raise ValueError("Incompatible pre-existing marketing suppression migration ledger.")
    prior_checksum = connection.execute(text("SELECT checksum FROM marketing_suppression_schema_migration WHERE migration_id = :id"), {"id": MIGRATION_ID}).scalar_one_or_none()
    if prior_checksum is not None:
        if prior_checksum != checksum:
            raise ValueError("Recorded marketing suppression migration checksum differs from this code revision.")
        validate_marketing_suppression_schema(connection)
        from app.services.email_migrations import history_rows, record_migration
        if history_rows(connection):
            record_migration(connection, MIGRATION_ID, state="applied", evidence="suppression checksum and schema validator passed")
        return False
    for statement in SUPPRESSION_DDL[1:]:
        connection.exec_driver_sql(statement)
    for statement in LEGACY_IMPORT_DDL:
        connection.exec_driver_sql(statement)
    connection.execute(text("INSERT INTO marketing_suppression_schema_migration (migration_id, checksum) VALUES (:id, :checksum)"), {"id": MIGRATION_ID, "checksum": checksum})
    validate_marketing_suppression_schema(connection)
    from app.services.email_migrations import history_rows, record_migration
    if history_rows(connection):
        record_migration(connection, MIGRATION_ID, state="applied", evidence="suppression migration applied and schema validator passed")
    return True


def marketing_account_lock_statement(account_id: int):
    """Build the single account-row lock used by marketing state/dispatch."""
    return select(CustomerAccount.id, CustomerAccount.active, CustomerAccount.marketing_status).where(
        CustomerAccount.id == account_id
    ).with_for_update()


def lock_marketing_account(account_id: int):
    """Lock the customer row used to serialize marketing-state transitions."""
    locked_id = db.session.execute(marketing_account_lock_statement(account_id)).scalar_one_or_none()
    return db.session.get(CustomerAccount, locked_id) if locked_id is not None else None


def record_suppression(
    account_id: int, *, source: str, reason_code: str, reason_text: str | None,
    actor_type: str, admin_id: int | None = None, provider_name: str | None = None,
    provider_message_id: str | None = None, provider_event_id: str | None = None,
    dedupe_fingerprint: str | None = None, metadata: dict | None = None,
) -> MarketingSuppressionCase | None:
    """Lock the account and atomically create/reuse its active case and event."""
    now = datetime.utcnow()
    account = lock_marketing_account(account_id)
    if account is None:
        return None
    idempotency_key = None
    if source == "provider" and provider_event_id:
        idempotency_key = f"provider-event:{provider_name or 'unknown'}:{provider_event_id}"
    elif source == "provider" and dedupe_fingerprint:
        idempotency_key = f"provider:{provider_name or 'unknown'}:{dedupe_fingerprint}"
    prior_event = MarketingSuppressionEvent.query.filter_by(idempotency_key=idempotency_key).first() if idempotency_key else None
    if prior_event:
        return db.session.execute(
            select(MarketingSuppressionCase).where(
                MarketingSuppressionCase.id == prior_event.suppression_case_id,
                MarketingSuppressionCase.cleared_at.is_(None),
            )
        ).scalar_one_or_none()
    account.marketing_status = "suppressed"
    # A sticky suppression is not represented as voluntary consent withdrawal.
    db.session.query(CustomerToken).filter(
        CustomerToken.account_id == account.id,
        CustomerToken.purpose.in_(("marketing_confirm", "welcome_opt_in")),
    ).filter(
        CustomerToken.used_at.is_(None)
    ).update({"used_at": now}, synchronize_session=False)
    case = db.session.execute(
        select(MarketingSuppressionCase).where(
            MarketingSuppressionCase.account_id == account.id,
            MarketingSuppressionCase.source == source,
            MarketingSuppressionCase.reason_code == reason_code,
            MarketingSuppressionCase.cleared_at.is_(None),
        ).with_for_update()
    ).scalar_one_or_none()
    created = case is None
    if created:
        case = MarketingSuppressionCase(
            account_id=account.id, source=source, reason_code=reason_code,
            detected_at=now, provider_name=provider_name,
            metadata_json=metadata or None,
        )
        db.session.add(case)
        db.session.flush()
    if source == "admin":
        idempotency_key = f"admin:{uuid4().hex}"
    if created or idempotency_key:
        db.session.add(MarketingSuppressionEvent(
            suppression_case_id=case.id, account_id=account.id,
            event_type="suppression_detected" if created else "suppression_observed",
            actor_type=actor_type, admin_id=admin_id, source=source,
            reason_code=reason_code, reason_text=reason_text,
            occurred_at=now, provider_name=provider_name,
            provider_event_id=provider_event_id,
            provider_message_id=provider_message_id,
            dedupe_fingerprint=dedupe_fingerprint,
            idempotency_key=idempotency_key,
            metadata_json=metadata or None,
        ))
    return case


def fingerprint_provider_event(event: dict, *, normalized_event: str, email: str) -> str | None:
    """Hash event content anchored by a provider message ID or event timestamp.

    Brevo's top-level ``id`` is not treated as an event ID. Arbitrary descriptive
    fields alone are insufficient for deduplication because distinct events can
    have identical descriptions.
    """
    if not isinstance(event, dict) or not normalized_event or not email or "event" not in event and "type" not in event:
        return None
    transport_normalized = {"id", "event", "type", "email", "recipient"}
    canonical = {key: value for key, value in event.items() if str(key).lower() not in transport_normalized}
    identity_keys = {"message-id", "messageid", "message_id", "ts_event", "ts"}
    if not any(
        str(key).lower() in identity_keys and value not in (None, "", [], {})
        for key, value in event.items()
    ):
        return None
    canonical["_normalized_event"] = normalized_event.strip().lower()
    canonical["_normalized_email"] = email.strip().lower()
    if not any(key not in {"event", "type", "email", "recipient", "_normalized_event", "_normalized_email"}
               and value not in (None, "", [], {}) for key, value in canonical.items()):
        return None
    try:
        serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def active_cases_for_accounts(account_ids: list[int]) -> dict[int, list[MarketingSuppressionCase]]:
    if not account_ids:
        return {}
    cases = MarketingSuppressionCase.query.filter(
        MarketingSuppressionCase.account_id.in_(account_ids),
        MarketingSuppressionCase.cleared_at.is_(None),
    ).order_by(MarketingSuppressionCase.detected_at.desc()).all()
    result: dict[int, list[MarketingSuppressionCase]] = {}
    for case in cases:
        result.setdefault(case.account_id, []).append(case)
    return result
