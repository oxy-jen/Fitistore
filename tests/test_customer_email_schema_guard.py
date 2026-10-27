from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine

from app.extensions import db
from app.models import Category, Product
from scripts.customer_email_schema_guard import (
    APPROVED_MISSING_INDEXES,
    APPROVED_MISSING_COLUMNS,
    BACKUP_CONFIRMATION,
    CONFIRMATION,
    PREFLIGHT_CONFIRMATION,
    ColumnSpec,
    GuardError,
    IdentityCheckError,
    classify_tables,
    _connect,
    _connection_failure_category,
    _engine_url,
    export_verified_migration_environment,
    migration_column_manifest,
    migration_column_defaults,
    migration_constraint_manifest,
    migration_index_manifest,
    require_confirmation,
    required_settings,
    run,
    _safe_driver_versions,
    validate_dispatch_request,
    validate_column_snapshot,
    validate_fk_options,
    validate_postgres_index_snapshot,
    validate_identity,
    validate_missing_state,
    validate_schema,
)


def test_confirmation_rejects_wrong_value_before_any_connection():
    with pytest.raises(GuardError, match="no database connection"):
        require_confirmation("wrong confirmation", BACKUP_CONFIRMATION)
    with pytest.raises(GuardError):
        require_confirmation(CONFIRMATION, "wrong backup confirmation")
    require_confirmation(CONFIRMATION, BACKUP_CONFIRMATION)


@pytest.mark.parametrize("operation,preflight,migration,backup,accepted", [
    ("preflight", PREFLIGHT_CONFIRMATION, None, None, True),
    ("preflight", "wrong", CONFIRMATION, BACKUP_CONFIRMATION, False),
    ("migrate", PREFLIGHT_CONFIRMATION, CONFIRMATION, BACKUP_CONFIRMATION, True),
    ("migrate", PREFLIGHT_CONFIRMATION, "wrong", BACKUP_CONFIRMATION, False),
    ("migrate", PREFLIGHT_CONFIRMATION, CONFIRMATION, "wrong", False),
    ("unknown", PREFLIGHT_CONFIRMATION, CONFIRMATION, BACKUP_CONFIRMATION, False),
])
def test_dispatch_confirmation_is_operation_specific(operation, preflight, migration, backup, accepted):
    if accepted:
        validate_dispatch_request(operation, preflight, migration, backup)
    else:
        with pytest.raises(GuardError):
            validate_dispatch_request(operation, preflight, migration, backup)


def test_workflow_restricts_manual_trigger_and_target_branch():
    workflow = Path(".github/workflows/migrate-customer-email-schema.yml").read_text()
    trigger_block = re.search(r"(?ms)^on:\s*\n(.*?)(?=^permissions:)", workflow)
    assert trigger_block
    events = re.findall(r"(?m)^  ([A-Za-z_][A-Za-z0-9_-]*):", trigger_block.group(1))
    assert events == ["workflow_dispatch"]
    jobs = re.split(r"(?m)^  (?:preflight|migrate):\s*$", workflow)
    assert len(jobs) == 3
    preflight_job = jobs[1]
    migrate_job = jobs[2]
    assert "github.ref == 'refs/heads/responsive-all-pages'" in preflight_job
    assert "github.ref == 'refs/heads/responsive-all-pages'" in migrate_job
    assert preflight_job.count("ref: ${{ github.sha }}") == 1
    assert migrate_job.count("ref: ${{ github.sha }}") == 1
    assert "ref: responsive-all-pages" not in preflight_job + migrate_job
    assert "inputs.operation == 'migrate'" in migrate_job
    assert "needs: preflight" in migrate_job
    migration_command = "python -m flask --app wsgi:app migrate-customer-email-schema"
    assert migration_command not in preflight_job
    assert migration_command in migrate_job
    assert "dispatch-check" in preflight_job and "dispatch-check" in migrate_job
    assert "PREFLIGHT_CONFIRMATION_INPUT: ${{ inputs.preflight_confirmation }}" in preflight_job
    assert "CONFIRMATION_INPUT: ${{ inputs.confirmation }}" in migrate_job
    assert "BACKUP_CONFIRMATION_INPUT: ${{ inputs.backup_confirmation }}" in migrate_job
    assert "RUNNER_TEMP" not in migrate_job
    assert "RENDER_DATABASE_CA_CERT" not in workflow
    assert "cleanup-ca" not in workflow
    command_step = migrate_job.split("- name: Apply the scoped customer email migration", 1)[1].split("- name:", 1)[0]
    assert "python -m flask --app wsgi:app migrate-customer-email-schema >/dev/null 2>&1" in command_step
    assert 'status=$?' in command_step and 'exit "$status"' in command_step
    assert "post-migration schema validation" in migrate_job
    assert command_step.index("exit \"$status\"") < migrate_job.index("post-migration schema validation")

    # Model the job dependency/gates: only a successful preflight plus a migrate
    # operation on the fixed ref can make the migration job eligible.
    def migration_job_eligible(operation, preflight_succeeded, ref):
        return (preflight_succeeded and ref == "refs/heads/responsive-all-pages"
                and operation == "migrate")
    assert not migration_job_eligible("preflight", True, "refs/heads/responsive-all-pages")
    assert not migration_job_eligible("preflight", False, "refs/heads/responsive-all-pages")
    assert not migration_job_eligible("migrate", False, "refs/heads/responsive-all-pages")
    assert not migration_job_eligible("migrate", True, "refs/heads/main")
    assert migration_job_eligible("migrate", True, "refs/heads/responsive-all-pages")


@pytest.mark.parametrize("preflight_succeeds", [True, False])
def test_preflight_only_never_has_an_executable_migration_path(preflight_succeeds):
    workflow = Path(".github/workflows/migrate-customer-email-schema.yml").read_text()
    jobs = re.split(r"(?m)^  (?:preflight|migrate):\s*$", workflow)
    preflight_job, migrate_job = jobs[1], jobs[2]
    migration_command = "python -m flask --app wsgi:app migrate-customer-email-schema"
    assert migration_command not in preflight_job
    gate_match = re.search(r"(?m)^    if:\s*\$\{\{\s*(.*?)\s*\}\}\s*$", migrate_job)
    assert gate_match
    gate_terms = {term.strip() for term in gate_match.group(1).split("&&")}
    assert gate_terms == {
        "needs.preflight.result == 'success'",
        "github.ref == 'refs/heads/responsive-all-pages'",
        "inputs.operation == 'migrate'",
    }
    assert "needs: preflight" in migrate_job
    # Evaluate the actual three-term gate for the preflight-only choice both
    # when its read-only check passes and when it fails.
    results = {
        "needs.preflight.result == 'success'": preflight_succeeds,
        "github.ref == 'refs/heads/responsive-all-pages'": True,
        "inputs.operation == 'migrate'": False,
    }
    assert all(results[term] for term in gate_terms) is False


def test_wrong_database_identity_is_rejected_without_disclosing_identity():
    with pytest.raises(GuardError, match="does not match"):
        validate_identity("other_db", "public", "expected_db", "public")


def _database_settings(**overrides):
    settings = {
        "DATABASE_URL": "postgresql://guard_user:unit-test-password@db.example.invalid:5432/store_db?sslmode=disable&sslrootcert=%2Ftmp%2Fwrong.pem",
        "EXPECTED_DATABASE_NAME": "store_db",
        "EXPECTED_DATABASE_SCHEMA": "public",
        "EXPECTED_DATABASE_HOST": "db.example.invalid",
    }
    settings.update(overrides)
    return settings


def test_required_settings_needs_no_ca_secret_and_checks_expected_identity():
    settings = required_settings(_database_settings())
    assert settings == (
        _database_settings()["DATABASE_URL"], "store_db", "public", "db.example.invalid"
    )
    with pytest.raises(GuardError, match="database name"):
        required_settings(_database_settings(EXPECTED_DATABASE_NAME="wrong_db"))
    with pytest.raises(GuardError, match="database host"):
        required_settings(_database_settings(EXPECTED_DATABASE_HOST="wrong.example.invalid"))


def test_engine_url_overrides_weaker_tls_query_settings():
    url = _engine_url(_database_settings()["DATABASE_URL"])
    assert "sslmode=verify-full" in url
    assert "sslrootcert=system" in url
    assert "sslmode=disable" not in url
    assert "/tmp/wrong.pem" not in url


def test_tls_environment_generation_uses_system_ca_without_creating_pem(tmp_path, monkeypatch, capsys):
    settings = _database_settings()
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_ENV", str(github_env))
    monkeypatch.delenv("DATABASE_CA_CERT", raising=False)

    export_verified_migration_environment()

    contents = github_env.read_text(encoding="utf-8")
    assert "sslmode=verify-full" in contents
    assert "sslrootcert=system" in contents
    assert "PGSSLMODE=verify-full" in contents
    assert "PGSSLROOTCERT=system" in contents
    assert "unit-test-password" not in capsys.readouterr().out
    assert not list(tmp_path.glob("*.pem"))


def test_read_only_connect_uses_system_ca_and_verify_full(monkeypatch):
    import sqlalchemy

    calls = {}

    class FakeDialect:
        name = "postgresql"

    class FakeConnection:
        dialect = FakeDialect()

        def exec_driver_sql(self, sql):
            assert sql == "SELECT current_database(), current_schema()"
            return type("Result", (), {"one": lambda self: ("store_db", "public")})()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

        def dispose(self):
            pass

    def fake_create_engine(url, **kwargs):
        calls["url"] = url
        calls.update(kwargs)
        return FakeEngine()

    monkeypatch.setattr(sqlalchemy, "create_engine", fake_create_engine)
    monkeypatch.setattr("scripts.customer_email_schema_guard.required_settings", lambda: (
        _database_settings()["DATABASE_URL"], "store_db", "public", "db.example.invalid"
    ))

    connection = _connect()
    assert calls["connect_args"] == {
        "sslmode": "verify-full", "sslrootcert": "system",
        "options": "-c default_transaction_read_only=on",
    }
    assert "sslmode=verify-full" in calls["url"]
    assert "sslrootcert=system" in calls["url"]


def test_preflight_error_reports_safe_driver_diagnostics_without_raw_error(monkeypatch):
    secret_url = "postgresql://private-user:private-password@private-host/private-db"
    raw_message = (
        f"connection failed for {secret_url}; token=private-token; "
        "address=192.0.2.44 database=private-db"
    )

    def fail_connect():
        raise RuntimeError(raw_message)

    monkeypatch.setattr("scripts.customer_email_schema_guard._connect", fail_connect)
    monkeypatch.setattr(
        "scripts.customer_email_schema_guard._safe_driver_versions",
        lambda: "psycopg=3.test implementation=binary libpq=180000",
    )
    with pytest.raises(GuardError) as caught:
        run("preflight")

    message = str(caught.value)
    assert "Database connection/identity check failed" in message
    assert "psycopg=3.test implementation=binary libpq=180000" in message
    assert "category=unclassified connection error" in message
    assert secret_url not in message
    assert "private-password" not in message
    assert "private-token" not in message
    assert "private-host" not in message
    assert "192.0.2.44" not in message
    assert "private-db" not in message
    assert raw_message not in message


@pytest.mark.parametrize("message,expected", [
    ("certificate verify failed for private-host", "TLS verification/configuration error"),
    ("connection timed out to private-host", "timeout error"),
    ("connection refused by 192.0.2.1", "network/connectivity error"),
    ("password authentication failed for user private-user", "authentication error"),
    ("unexpected private detail", "unclassified connection error"),
])
def test_connection_failure_category_never_returns_exception_text(message, expected):
    error = RuntimeError(message)
    category = _connection_failure_category(error)
    assert category == expected
    assert "private-host" not in category
    assert "private-user" not in category


def test_connection_failure_category_uses_sqlstate_without_message():
    class DatabaseError(Exception):
        sqlstate = "28P01"

        def __str__(self):
            return "private-user private-password"

    assert _connection_failure_category(DatabaseError()) == "authentication error"


@pytest.mark.parametrize("sqlstate,expected", [
    ("28P01", "authentication error"),
    ("08001", "network/connectivity error"),
    ("08006", "network/connectivity error"),
    ("ZZZZZ", "unclassified connection error"),
])
def test_recognized_sqlstate_maps_to_safe_category(sqlstate, expected):
    class DatabaseError(Exception):
        def __init__(self, state):
            self.sqlstate = state

        def __str__(self):
            return "message includes private-password and private-host"

    assert _connection_failure_category(DatabaseError(sqlstate)) == expected


def test_driver_version_diagnostics_use_mocked_local_versions(monkeypatch):
    psycopg = ModuleType("psycopg")
    psycopg.__version__ = "3.2.7"
    psycopg.pq = SimpleNamespace(__impl__="binary", version=lambda: 180000)
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)

    assert _safe_driver_versions() == "psycopg=3.2.7 implementation=binary libpq=180000"


def test_missing_driver_version_information_is_reported_safely(monkeypatch):
    psycopg = ModuleType("psycopg")
    psycopg.__version__ = "3.2.7"
    psycopg.pq = SimpleNamespace(__impl__="binary")
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)

    assert _safe_driver_versions() == "psycopg=3.2.7 implementation=binary libpq=unavailable"


def test_preflight_identity_failure_has_safe_identity_category(monkeypatch):
    monkeypatch.setattr(
        "scripts.customer_email_schema_guard._connect",
        lambda: (_ for _ in ()).throw(IdentityCheckError("private database identity")),
    )
    monkeypatch.setattr(
        "scripts.customer_email_schema_guard._safe_driver_versions",
        lambda: "psycopg=3.test implementation=binary libpq=180000",
    )
    with pytest.raises(GuardError) as caught:
        run("preflight")
    assert "category=identity check failed" in str(caught.value)
    assert "private database identity" not in str(caught.value)


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


@pytest.mark.parametrize("table,column", sorted(APPROVED_MISSING_COLUMNS))
def test_each_approved_missing_column_is_allowed_only_during_preflight(table, column):
    expected = {column, "present_column"}
    actual = {"present_column"}
    assert validate_missing_state(table, expected, actual, {column}, True, "columns") == {column}
    with pytest.raises(GuardError, match="missing"):
        validate_missing_state(table, expected, actual, {column}, False, "columns")


def test_approved_provider_message_index_is_allowed_only_during_preflight():
    index = next(iter(APPROVED_MISSING_INDEXES))
    assert validate_missing_state("email_message", {index, "other"}, {"other"}, {index}, True, "indexes") == {index}
    with pytest.raises(GuardError, match="missing"):
        validate_missing_state("email_message", {index}, set(), {index}, False, "indexes")


def test_unexpected_missing_object_is_rejected_even_during_preflight():
    with pytest.raises(GuardError, match="Unexpected missing"):
        validate_missing_state("email_message", {"unexpected"}, set(), set(), True, "columns")


def test_exact_column_type_length_nullability_and_default_checks():
    email = migration_column_manifest()[("customer_account", "email")]
    validate_column_snapshot("customer_account", "email", "VARCHAR(254)", False, None, email)
    with pytest.raises(GuardError, match="type"):
        validate_column_snapshot("customer_account", "email", "VARCHAR(255)", False, None, email)
    with pytest.raises(GuardError, match="type"):
        validate_column_snapshot("customer_account", "email", "TEXT", False, None, email)
    with pytest.raises(GuardError, match="nullability"):
        validate_column_snapshot("customer_account", "email", "VARCHAR(254)", True, None, email)
    with pytest.raises(GuardError, match="default"):
        validate_column_snapshot("customer_account", "email", "VARCHAR(254)", False, "'wrong'", email)


def test_timestamp_timezone_and_numeric_precision_are_checked():
    timestamp = ColumnSpec("TIMESTAMP WITHOUT TIME ZONE", True, None)
    validate_column_snapshot("example", "moment", "TIMESTAMP WITHOUT TIME ZONE", True, None, timestamp)
    with pytest.raises(GuardError, match="type"):
        validate_column_snapshot("example", "moment", "TIMESTAMP WITH TIME ZONE", True, None, timestamp)
    numeric = ColumnSpec("NUMERIC(10,2)", True, None)
    with pytest.raises(GuardError, match="type"):
        validate_column_snapshot("example", "amount", "NUMERIC(10,3)", True, None, numeric)


def test_foreign_key_match_mode_and_deferrability_are_checked():
    validate_fk_options({})
    validate_fk_options({"match": "SIMPLE", "deferrable": False, "initially": "IMMEDIATE"})
    with pytest.raises(GuardError, match="match mode"):
        validate_fk_options({"match": "FULL"})
    with pytest.raises(GuardError, match="deferrable"):
        validate_fk_options({"deferrable": True})
    with pytest.raises(GuardError, match="timing mode"):
        validate_fk_options({"initially": "DEFERRED"})


def _index_snapshot(**changes):
    snapshot = {
        "name": "ix_sample", "table": "sample", "columns": ("account_id",),
        "unique": False, "valid": True, "has_predicate": False,
        "included_columns": (), "access_method": "btree",
        "key_count": 1, "total_count": 1,
    }
    snapshot.update(changes)
    return snapshot


def test_postgres_index_catalog_snapshot_accepts_expected_plain_btree():
    validate_postgres_index_snapshot(
        "ix_sample", "sample", ("account_id",), False, _index_snapshot()
    )


@pytest.mark.parametrize("changes", [
    {"has_predicate": True},
    {"included_columns": ("created_at",), "total_count": 2},
    {"access_method": "hash"},
    {"columns": ("created_at",)},
    {"unique": True},
    {"valid": False},
    {"table": "other_table"},
])
def test_postgres_index_catalog_snapshot_rejects_incompatible_indexes(changes):
    with pytest.raises(GuardError, match="Incompatible PostgreSQL index"):
        validate_postgres_index_snapshot(
            "ix_sample", "sample", ("account_id",), False, _index_snapshot(**changes)
        )


def test_postgres_index_catalog_snapshot_fails_closed_on_missing_metadata():
    incomplete = _index_snapshot()
    del incomplete["access_method"]
    with pytest.raises(GuardError, match="metadata incomplete"):
        validate_postgres_index_snapshot("ix_sample", "sample", ("account_id",), False, incomplete)


def test_unrelated_additional_indexes_are_allowed_by_policy():
    # The guard validates every migration-declared name but tolerates unrelated
    # local indexes; this avoids rejecting safe operator-created performance indexes.
    assert validate_missing_state(
        "sample", {"ix_expected"}, {"ix_expected", "ix_operator_added"}, set(), False, "indexes"
    ) == set()


def test_manifest_reads_the_actual_sql_indexes_and_defaults():
    indexes = migration_index_manifest()
    defaults = migration_column_defaults()
    unique, named_unique, foreign_keys = migration_constraint_manifest()
    assert indexes["ix_marketing_campaign_run_campaign_key"] == (
        "marketing_campaign_run", ("campaign_key",), True
    )
    assert defaults[("customer_account", "two_factor_enabled")] == "FALSE"
    assert defaults[("email_message", "retry_count")] == "0"
    assert unique["customer_order_link"] == {("order_id",)}
    assert named_unique["email_campaign_recipient"]["uq_campaign_recipient_account"] == ("campaign_id", "account_id")
    assert ("account_id", "customer_account", "id") in foreign_keys["email_message"]


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
