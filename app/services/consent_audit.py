"""Append-only database protection for customer consent history."""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import inspect, text

MIGRATION_ID = "20261117_customer_consent_audit_immutable"
_MIGRATION_FILE = Path(__file__).resolve().parents[2] / "migrations" / f"{MIGRATION_ID}.sql"
ERASURE_MIGRATION_ID = "20261124_customer_data_erasure_guard"
_ERASURE_MIGRATION_FILE = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / f"{ERASURE_MIGRATION_ID}.sql"
)

_POSTGRES_ACCOUNT_MARKER = """
account.id::text = erasure_account_id
AND account.email = 'removed-customer-' || account.id::text || '@redacted.invalid'
AND account.display_name = 'Deleted customer'
AND account.password_hash IS NULL
AND account.google_sub IS NULL
AND account.email_verified_at IS NULL
AND account.two_factor_enabled IS FALSE
AND account.marketing_status = 'unsubscribed'
AND account.marketing_consent_at IS NULL
AND account.marketing_withdrawn_at IS NULL
AND account.active IS FALSE
"""

_SQLITE_ACCOUNT_MARKER = """
account.email = 'removed-customer-' || account.id || '@redacted.invalid'
AND account.display_name = 'Deleted customer'
AND account.password_hash IS NULL
AND account.google_sub IS NULL
AND account.email_verified_at IS NULL
AND account.two_factor_enabled = 0
AND account.marketing_status = 'unsubscribed'
AND account.marketing_consent_at IS NULL
AND account.marketing_withdrawn_at IS NULL
AND account.active = 0
"""

_ERASURE_ROW_CONDITIONS = {
    "customer_consent_event": "account.id = OLD.account_id",
    "marketing_suppression_event": "account.id = OLD.account_id",
    "email_delivery_investigation_event": """
        EXISTS (
          SELECT 1 FROM email_delivery_investigation AS investigation
          JOIN email_message AS message ON message.id = investigation.message_id
          WHERE investigation.id = OLD.investigation_id
            AND message.account_id = account.id
        )
    """,
    "email_legacy_payload_review_event": """
        EXISTS (
          SELECT 1 FROM email_message AS message
          WHERE message.id = OLD.message_id AND message.account_id = account.id
        )
    """,
}

_ERASURE_FUNCTION_NAMES = {
    "customer_consent_event": "fity_reject_customer_consent_event_mutation",
    "marketing_suppression_event": "fity_reject_suppression_event_mutation",
    "email_delivery_investigation_event": "fity_reject_email_investigation_event_mutation",
    "email_legacy_payload_review_event": "fity_reject_email_legacy_review_mutation",
}

_ERASURE_TRIGGER_NAMES = {
    "customer_consent_event": (
        "trg_customer_consent_event_immutable",
        "trg_customer_consent_event_no_truncate",
    ),
    "marketing_suppression_event": (
        "trg_marketing_suppression_event_immutable",
        "trg_marketing_suppression_event_no_truncate",
    ),
    "email_delivery_investigation_event": (
        "trg_email_investigation_event_immutable",
        "trg_email_investigation_event_no_truncate",
    ),
    "email_legacy_payload_review_event": (
        "trg_email_legacy_review_immutable",
        "trg_email_legacy_review_no_truncate",
    ),
}

_SQLITE_DELETE_TRIGGERS = {
    "customer_consent_event": "trg_customer_consent_event_immutable_delete",
    "marketing_suppression_event": "trg_marketing_suppression_event_immutable",
    "email_delivery_investigation_event": "trg_email_investigation_event_no_delete",
    "email_legacy_payload_review_event": "trg_email_legacy_review_no_delete",
}


def _postgres_erasure_function(table_name: str) -> str:
    return f"""
DECLARE
  erasure_account_id TEXT;
BEGIN
  erasure_account_id := current_setting('fity.customer_erasure_account_id', true);
  IF TG_OP = 'DELETE' AND erasure_account_id IS NOT NULL THEN
    IF EXISTS (
      SELECT 1 FROM customer_account AS account
      WHERE {_POSTGRES_ACCOUNT_MARKER}
        AND ({_ERASURE_ROW_CONDITIONS[table_name]})
    ) THEN
      RETURN OLD;
    END IF;
  END IF;
  RAISE EXCEPTION '{_append_only_message(table_name)}';
END;
"""


def _append_only_message(table_name: str) -> str:
    return {
        "customer_consent_event": "customer consent events are append-only",
        "marketing_suppression_event": "marketing suppression events are append-only",
        "email_delivery_investigation_event": "email investigation events are append-only",
        "email_legacy_payload_review_event": "legacy email review events are append-only",
    }[table_name]


def _sqlite_delete_trigger(table_name: str) -> str:
    trigger_name = _SQLITE_DELETE_TRIGGERS[table_name]
    row_condition = _ERASURE_ROW_CONDITIONS[table_name]
    return f"""
CREATE TRIGGER {trigger_name}
BEFORE DELETE ON {table_name}
WHEN NOT EXISTS (
  SELECT 1 FROM customer_account AS account
  WHERE {_SQLITE_ACCOUNT_MARKER}
    AND ({row_condition})
)
BEGIN
  SELECT RAISE(ABORT, '{_append_only_message(table_name)}');
END
"""


_ERASURE_SQLITE_UPDATE_TRIGGERS = {
    "customer_consent_event": (
        "trg_customer_consent_event_immutable_update",
        "customer consent events are append-only",
    ),
    "marketing_suppression_event": (
        "trg_marketing_suppression_event_immutable_update",
        "marketing suppression events are append-only",
    ),
    "email_delivery_investigation_event": (
        "trg_email_investigation_event_immutable",
        "email investigation events are append-only",
    ),
    "email_legacy_payload_review_event": (
        "trg_email_legacy_review_immutable",
        "legacy email review events are append-only",
    ),
}


def migration_checksum() -> str:
    return hashlib.sha256(_MIGRATION_FILE.read_bytes()).hexdigest()


def erasure_migration_checksum() -> str:
    return hashlib.sha256(_ERASURE_MIGRATION_FILE.read_bytes()).hexdigest()


def erasure_guard_migration_applied(connection) -> bool:
    if not inspect(connection).has_table("email_schema_migration"):
        return False
    recorded = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:migration_id"
    ), {"migration_id": ERASURE_MIGRATION_ID}).scalar_one_or_none()
    if recorded is None:
        return False
    if recorded != erasure_migration_checksum():
        raise ValueError("Customer data erasure migration checksum is missing or incompatible.")
    return True


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
    erasure_ledger = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:migration_id"
    ), {"migration_id": ERASURE_MIGRATION_ID}).scalar_one_or_none()
    erasure_applied = erasure_ledger is not None
    if erasure_applied and erasure_ledger != erasure_migration_checksum():
        raise ValueError(
            "Customer consent erasure migration checksum is missing or incompatible."
        )
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
            expected_source = (
                "".join(_postgres_erasure_function("customer_consent_event").lower().split())
                if erasure_applied
                else "beginraiseexception'customerconsenteventsareappend-only';end;"
            )
            if (not row or row["tgenabled"] != "O" or row["tgtype"] != event_mask
                    or row["proname"] != "fity_reject_customer_consent_event_mutation"
                    or row["return_type"] != "trigger" or row["nspname"] != schema_name
                    or source != expected_source):
                raise ValueError(f"Customer consent append-only trigger {name} is missing or incompatible.")
    elif connection.dialect.name == "sqlite":
        rows = connection.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='customer_consent_event'"
        )).mappings().all()
        actual = {row["name"]: "".join((row["sql"] or "").lower().split()) for row in rows}
        for name, operation in (("trg_customer_consent_event_immutable_update", "beforeupdate"),
                                ("trg_customer_consent_event_immutable_delete", "beforedelete")):
            definition = actual.get(name, "")
            expected_delete = (
                "".join(
                    _sqlite_delete_trigger("customer_consent_event").lower().split()
                )
                if erasure_applied and name == "trg_customer_consent_event_immutable_delete"
                else None
            )
            if (operation not in definition
                    or "raise(abort,'customerconsenteventsareappend-only')" not in definition
                    or (expected_delete is not None and definition != expected_delete)):
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


def validate_customer_data_erasure_schema(connection) -> None:
    inspector = inspect(connection)
    if not inspector.has_table("customer_account"):
        raise ValueError("Customer account table is required by the erasure guard.")
    if not inspector.has_table("customer_consent_event"):
        raise ValueError("Customer consent event table is required by the erasure guard.")
    if connection.dialect.name == "postgresql":
        schema_name = connection.execute(text("SELECT current_schema()")).scalar_one()
        rows = connection.execute(text("""
            SELECT t.tgname, t.tgenabled, t.tgtype, p.proname, p.prosrc,
                   p.prorettype::regtype::text AS return_type, c.relname, n.nspname
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_proc p ON p.oid=t.tgfoid
            WHERE n.nspname=:schema_name AND NOT t.tgisinternal
        """), {"schema_name": schema_name}).mappings().all()
        actual = {row["tgname"]: row for row in rows}
        for table_name, trigger_names in _ERASURE_TRIGGER_NAMES.items():
            if not inspector.has_table(table_name, schema=schema_name):
                if table_name == "marketing_suppression_event":
                    continue
                raise ValueError(f"Required customer erasure table {table_name} is missing.")
            for name, event_mask in zip(trigger_names, (27, 34)):
                row = actual.get(name)
                expected_source = "".join(
                    _postgres_erasure_function(table_name).lower().split()
                )
                source = "".join(str(row["prosrc"]).lower().split()) if row else ""
                if (not row or row["tgenabled"] != "O" or row["tgtype"] != event_mask
                        or row["proname"] != _ERASURE_FUNCTION_NAMES[table_name]
                        or row["return_type"] != "trigger"
                        or row["relname"] != table_name
                        or row["nspname"] != schema_name
                        or source != expected_source):
                    raise ValueError(f"Customer erasure guard trigger {name} is missing or incompatible.")
    elif connection.dialect.name == "sqlite":
        rows = connection.execute(text(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"
        )).mappings().all()
        actual = {row["name"]: "".join((row["sql"] or "").lower().split()) for row in rows}
        for table_name, delete_name in _SQLITE_DELETE_TRIGGERS.items():
            if not inspector.has_table(table_name):
                if table_name == "marketing_suppression_event":
                    continue
                raise ValueError(f"Required customer erasure table {table_name} is missing.")
            definition = actual.get(delete_name, "")
            expected_delete = "".join(_sqlite_delete_trigger(table_name).lower().split())
            if definition != expected_delete:
                raise ValueError(f"SQLite customer erasure guard {delete_name} is missing or incompatible.")
            update_name, update_message = _ERASURE_SQLITE_UPDATE_TRIGGERS[table_name]
            update_definition = actual.get(update_name, "")
            expected_update_error = "".join(
                f"raise(abort,'{update_message}')".split()
            )
            if ("beforeupdate" not in update_definition
                    or expected_update_error not in update_definition):
                raise ValueError(f"SQLite append-only update guard {update_name} is missing or incompatible.")
    else:
        raise ValueError("Unsupported customer erasure guard dialect.")


def apply_customer_data_erasure_migration(connection, dialect: str) -> bool:
    """Permit linked audit deletion only in a marked, transaction-authorized purge."""
    if dialect not in {"sqlite", "postgresql", "postgres"}:
        raise ValueError("Unsupported customer data erasure migration dialect.")
    inspector = inspect(connection)
    if not inspector.has_table("email_schema_migration"):
        raise ValueError("The email schema migration ledger must exist before this migration.")
    account_columns = {
        row["name"] for row in inspector.get_columns("customer_account")
    }
    required_account_columns = {
        "id", "email", "display_name", "password_hash", "google_sub",
        "email_verified_at", "two_factor_enabled", "marketing_status",
        "marketing_consent_at", "marketing_withdrawn_at", "active",
    }
    if not required_account_columns <= account_columns:
        raise ValueError("Customer account schema is incomplete for permanent erasure.")
    required_tables = {
        "customer_consent_event", "email_delivery_investigation",
        "email_delivery_investigation_event", "email_legacy_payload_review_event",
        "email_message",
    }
    missing = required_tables - set(inspector.get_table_names())
    if missing:
        raise ValueError(
            "Required customer erasure table(s) are missing: " + ", ".join(sorted(missing))
        )

    checksum = erasure_migration_checksum()
    prior = connection.execute(text(
        "SELECT checksum FROM email_schema_migration WHERE migration_id=:migration_id"
    ), {"migration_id": ERASURE_MIGRATION_ID}).scalar_one_or_none()
    if prior is not None and prior != checksum:
        raise ValueError("Recorded customer data erasure migration checksum is incompatible.")

    if prior is None:
        validate_consent_audit_schema(connection)
        connection.execute(text(
            "INSERT INTO email_schema_migration (migration_id, checksum) "
            "VALUES (:migration_id, :checksum)"
        ), {"migration_id": ERASURE_MIGRATION_ID, "checksum": checksum})

    protected_tables = [
        "customer_consent_event",
        "email_delivery_investigation_event",
        "email_legacy_payload_review_event",
    ]
    if inspector.has_table("marketing_suppression_event"):
        protected_tables.append("marketing_suppression_event")
    if dialect in {"postgresql", "postgres"}:
        for table_name in protected_tables:
            connection.execute(text(
                f"CREATE OR REPLACE FUNCTION {_ERASURE_FUNCTION_NAMES[table_name]}() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                + _postgres_erasure_function(table_name)
                + " $$"
            ))
    else:
        for table_name in protected_tables:
            trigger_name = _SQLITE_DELETE_TRIGGERS[table_name]
            connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
            connection.exec_driver_sql(_sqlite_delete_trigger(table_name))
            update_name, update_message = _ERASURE_SQLITE_UPDATE_TRIGGERS[table_name]
            connection.exec_driver_sql(f"""
                CREATE TRIGGER IF NOT EXISTS {update_name}
                BEFORE UPDATE ON {table_name}
                BEGIN SELECT RAISE(ABORT, '{update_message}'); END
            """)

    validate_customer_data_erasure_schema(connection)
    validate_consent_audit_schema(connection)
    from app.services.email_migrations import history_rows, record_migration
    if history_rows(connection):
        record_migration(
            connection,
            ERASURE_MIGRATION_ID,
            state="applied",
            evidence="linked append-only audit rows remain immutable except during authorized account erasure",
        )
    return prior is None
