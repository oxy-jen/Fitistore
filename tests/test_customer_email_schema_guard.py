from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app import customer_email_schema_tables
from app.extensions import db
from app.models import Category, Product
from scripts.customer_email_schema_guard import (
    APPROVED_MISSING_INDEXES,
    GuardError,
    classify_tables,
    migration_column_defaults,
    migration_index_manifest,
    require_confirmation,
    validate_identity,
    validate_schema,
)


def test_confirmation_rejects_wrong_value_before_any_connection():
    with pytest.raises(GuardError, match="no database connection"):
        require_confirmation("wrong confirmation", "I HAVE A VERIFIED RESTORABLE BACKUP")


def test_workflow_restricts_manual_trigger_and_target_branch():
    workflow = Path(".github/workflows/migrate-customer-email-schema.yml").read_text()
    assert "workflow_dispatch:" in workflow
    assert "if: ${{ github.ref == 'refs/heads/responsive-all-pages' }}" in workflow
    assert "python scripts/customer_email_schema_guard.py confirm" in workflow
    assert "BACKUP_CONFIRMATION_INPUT: ${{ inputs.backup_confirmation }}" in workflow


def test_wrong_database_identity_is_rejected_without_disclosing_identity():
    with pytest.raises(GuardError, match="does not match"):
        validate_identity("other_db", "public", "expected_db", "public")


def test_missing_parent_table_stops_preflight():
    engine = create_engine("sqlite://")
    db.metadata.create_all(engine, tables=[Category.__table__, Product.__table__])
    try:
        with engine.connect() as connection, pytest.raises(GuardError, match="parent tables are missing"):
            validate_schema(connection, "public", allow_approved_missing=True)
    finally:
        engine.dispose()


def test_unexpected_partial_table_set_is_rejected():
    with pytest.raises(GuardError, match="ambiguous partial"):
        classify_tables({"customer_account"})


def test_manifest_reads_the_actual_sql_indexes_and_defaults():
    indexes = migration_index_manifest()
    defaults = migration_column_defaults()
    assert indexes["ix_marketing_campaign_run_campaign_key"] == (
        "marketing_campaign_run", ("campaign_key",), True
    )
    assert defaults[("customer_account", "two_factor_enabled")] == "FALSE"
    assert defaults[("email_message", "retry_count")] == "0"


def test_complete_local_schema_passes_strict_postcheck():
    engine = create_engine("sqlite://")
    db.metadata.create_all(engine)
    try:
        with engine.connect() as connection:
            assert validate_schema(connection, "public", allow_approved_missing=False)
    finally:
        engine.dispose()


def test_postcheck_fails_when_required_index_is_missing():
    engine = create_engine("sqlite://")
    db.metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"DROP INDEX {next(iter(APPROVED_MISSING_INDEXES))}")
        with engine.connect() as connection, pytest.raises(GuardError, match="missing index"):
            validate_schema(connection, "public", allow_approved_missing=False)
    finally:
        engine.dispose()
