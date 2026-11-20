"""Shared coordination and checksum history for customer/email migrations.

This module deliberately records reconciled legacy schemas separately from
migrations executed by this code. A matching current schema cannot prove which
historical file revision was originally used.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import inspect, text

MIGRATION_LOCK_ID = 724930117
MIGRATION_HISTORY_ID = "20261117_email_migration_history"
MIGRATION_HISTORY_FILE = (
    Path(__file__).resolve().parents[2] / "migrations" / f"{MIGRATION_HISTORY_ID}.sql"
)
HISTORICAL_MIGRATIONS = (
    "20261017_customer_accounts_email",
    "20261027_email_studio",
    "20261028_email_builder",
    "20261029_consent_wording_version",
)
CUSTOMER_EMAIL_ADDITIVE_MIGRATIONS = (
    "20261113_email_outbox",
    "20261117_email_admin_test_purpose",
    "20261116_email_delivery_ops",
    "20261117_email_legacy_review",
    "20261117_customer_consent_audit_immutable",
    "20261120_marketing_campaign_queue",
)
HISTORY_TABLE = "email_migration_history"
_HISTORY_DDL = """CREATE TABLE IF NOT EXISTS email_migration_history (
    migration_id VARCHAR(120) PRIMARY KEY,
    checksum VARCHAR(64) NOT NULL,
    state VARCHAR(24) NOT NULL CONSTRAINT ck_email_migration_history_state CHECK (state IN ('applied', 'reconciled_schema')),
    evidence TEXT NOT NULL DEFAULT '',
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""


class MigrationLockBusy(RuntimeError):
    """Another related migration currently owns the shared transaction lock."""


def acquire_migration_lock(connection) -> None:
    """Acquire the shared PG transaction lock or fail immediately and clearly."""
    if connection.dialect.name not in {"postgresql", "postgres"}:
        return
    acquired = connection.execute(
        text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
        {"lock_id": MIGRATION_LOCK_ID},
    ).scalar_one()
    if not acquired:
        raise MigrationLockBusy(
            "Another customer/email migration is running. No changes were made; retry after it finishes."
        )


def migration_checksum(migration_id: str) -> str:
    if migration_id == "20261113_marketing_suppression":
        from app.services.marketing_suppressions import migration_checksum as checksum
        return checksum()
    path = Path(__file__).resolve().parents[2] / "migrations" / f"{migration_id}.sql"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def history_checksum() -> str:
    return hashlib.sha256(MIGRATION_HISTORY_FILE.read_bytes()).hexdigest()


def ensure_history_table(connection) -> None:
    """Create/check the independent central ledger inside caller transaction."""
    connection.execute(text(_HISTORY_DDL))
    inspector = inspect(connection)
    columns = {row["name"]: row for row in inspector.get_columns(HISTORY_TABLE)}
    expected = {"migration_id", "checksum", "state", "evidence", "recorded_at"}
    if set(columns) != expected:
        raise ValueError("Email migration history table has an incompatible column set.")
    expected_types = {
        "migration_id": "VARCHAR(120)", "checksum": "VARCHAR(64)",
        "state": "VARCHAR(24)", "evidence": "TEXT",
        "recorded_at": "TIMESTAMP WITHOUT TIME ZONE" if connection.dialect.name == "postgresql" else "TIMESTAMP",
    }
    for name, column in columns.items():
        actual_type = column["type"].compile(dialect=connection.dialect).upper()
        if actual_type != expected_types[name] or (column["nullable"] and not (connection.dialect.name == "sqlite" and name == "migration_id")):
            raise ValueError(f"Email migration history column {name} is incompatible.")
    if tuple(inspector.get_pk_constraint(HISTORY_TABLE).get("constrained_columns") or ()) != ("migration_id",):
        raise ValueError("Email migration history table has an incompatible primary key.")
    checks = inspector.get_check_constraints(HISTORY_TABLE)
    if len(checks) != 1 or checks[0].get("name") != "ck_email_migration_history_state":
        raise ValueError("Email migration history state constraint is missing or unexpected.")
    normalized = "".join((checks[0].get("sqltext") or "").lower().replace('"', "").split())
    normalized = normalized.replace("::charactervarying", "").replace("::text[]", "").replace("::text", "")
    normalized = normalized.replace("check", "").replace("(", "").replace(")", "")
    accepted = {
        "statein'applied','reconciled_schema'",
        "state=anyarray['applied','reconciled_schema']",
    }
    if normalized not in accepted:
        raise ValueError("Email migration history state constraint is altered.")


def history_rows(connection) -> dict[str, dict[str, str]]:
    if not inspect(connection).has_table(HISTORY_TABLE):
        return {}
    return {
        row["migration_id"]: dict(row)
        for row in connection.execute(text(
            "SELECT migration_id, checksum, state, evidence, recorded_at FROM email_migration_history"
        )).mappings()
    }


def record_migration(connection, migration_id: str, *, state: str, evidence: str) -> None:
    if state not in {"applied", "reconciled_schema"}:
        raise ValueError("Invalid migration history state.")
    checksum = history_checksum() if migration_id == MIGRATION_HISTORY_ID else migration_checksum(migration_id)
    previous = history_rows(connection).get(migration_id)
    if previous:
        if previous["checksum"] != checksum:
            raise ValueError(f"Migration checksum mismatch for {migration_id}.")
        if previous["state"] != state and previous["state"] == "applied":
            return
        return
    connection.execute(text(
        "INSERT INTO email_migration_history (migration_id, checksum, state, evidence) "
        "VALUES (:migration_id, :checksum, :state, :evidence)"
    ), {"migration_id": migration_id, "checksum": checksum, "state": state, "evidence": evidence[:1000]})


def validate_history_checksums(connection) -> list[str]:
    """Reject altered known entries and unexpected ledger IDs; report omissions."""
    rows = history_rows(connection)
    expected_ids = {
        MIGRATION_HISTORY_ID, *HISTORICAL_MIGRATIONS,
        "20261113_email_outbox", "20261116_email_delivery_ops",
        "20261113_marketing_suppression", "20261117_email_legacy_review",
        "20261117_customer_consent_audit_immutable", "20261117_email_admin_test_purpose",
        "20261120_marketing_campaign_queue",
    }
    unexpected = set(rows) - expected_ids
    if unexpected:
        raise ValueError("Unexpected email migration history entries: " + ", ".join(sorted(unexpected)))
    mismatches = []
    for migration_id, row in rows.items():
        expected = history_checksum() if migration_id == MIGRATION_HISTORY_ID else migration_checksum(migration_id)
        if row["checksum"] != expected:
            mismatches.append(migration_id)
    if mismatches:
        raise ValueError("Email migration history checksum mismatch: " + ", ".join(sorted(mismatches)))
    for migration_id, row in rows.items():
        allowed_states = (
            {"applied", "reconciled_schema"}
            if migration_id in HISTORICAL_MIGRATIONS
            else {"applied"}
        )
        if row["state"] not in allowed_states:
            raise ValueError(f"Email migration history state is invalid for {migration_id}.")
    return sorted(set(expected_ids) - set(rows))


def validate_customer_email_history(connection, *, require_complete: bool = False) -> None:
    """Require checksummed customer/email history entries in valid states."""
    if not inspect(connection).has_table(HISTORY_TABLE):
        raise ValueError("Customer/email migration history table is missing.")
    ensure_history_table(connection)
    missing = set(validate_history_checksums(connection))
    required = {MIGRATION_HISTORY_ID, *HISTORICAL_MIGRATIONS}
    if require_complete:
        required.update(CUSTOMER_EMAIL_ADDITIVE_MIGRATIONS)
    missing_required = sorted(required & missing)
    if missing_required:
        raise ValueError(
            "Required customer/email migration history entries are missing: "
            + ", ".join(missing_required)
        )


def verify_legacy_schema(connection, schema: str | None = None) -> list[str]:
    """Require the complete historical schema before reconciling legacy state."""
    from scripts.customer_email_schema_guard import validate_historical_schema

    return validate_historical_schema(connection, schema or "public")


def current_table_names(connection) -> set[str]:
    return set(inspect(connection).get_table_names())


def has_legacy_email_schema(connection) -> bool:
    return bool(current_table_names(connection) & {
        "customer_account", "customer_token", "customer_consent_event", "email_message",
        "email_template", "email_draft", "marketing_campaign_run",
    })
