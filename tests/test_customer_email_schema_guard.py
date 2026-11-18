from pathlib import Path
import os
import re
import ssl
import sys
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

from app.extensions import db
from app.models import Category, Product
from scripts.customer_email_schema_guard import (
    APPROVED_MISSING_TABLES,
    APPROVED_MISSING_INDEXES,
    APPROVED_MISSING_COLUMNS,
    BACKUP_CONFIRMATION,
    CONFIRMATION,
    EMAIL_STUDIO_TABLES,
    EMAIL_DELIVERY_OPS_TABLES,
    HISTORICAL_REQUIRED_TABLES,
    PREFLIGHT_CONFIRMATION,
    PRE_EMAIL_STUDIO_TABLES,
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
    safe_failure,
    validate_dispatch_request,
    validate_column_snapshot,
    validate_fk_options,
    validate_historical_schema,
    validate_postgres_foreign_key_catalog,
    validate_postgres_index_snapshot,
    postgres_foreign_key_catalog,
    postgres_index_catalog,
    validate_identity,
    validate_missing_state,
    validate_schema,
    write_summary,
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
    assert "PYTHONPATH: ${{ github.workspace }}" in preflight_job
    assert "PYTHONPATH: ${{ github.workspace }}" in migrate_job
    assert "github.ref == 'refs/heads/main'" in preflight_job
    assert "github.ref == 'refs/heads/main'" in migrate_job
    assert preflight_job.count("ref: ${{ github.sha }}") == 1
    assert migrate_job.count("ref: ${{ github.sha }}") == 1
    assert "ref: responsive-all-pages" not in preflight_job + migrate_job
    assert "inputs.operation == 'migrate'" in migrate_job
    assert "needs: preflight" in migrate_job
    migration_command = "python -m flask --app manage:app migrate-customer-email-schema"
    assert migration_command not in preflight_job
    assert migration_command in migrate_job
    assert "dispatch-check" in preflight_job and "dispatch-check" in migrate_job
    assert "PREFLIGHT_CONFIRMATION_INPUT: ${{ inputs.preflight_confirmation }}" in preflight_job
    assert "CONFIRMATION_INPUT: ${{ inputs.confirmation }}" in migrate_job
    assert "BACKUP_CONFIRMATION_INPUT: ${{ inputs.backup_confirmation }}" in migrate_job
    assert "RUNNER_TEMP" not in migrate_job
    for setting in (
        "DATABASE_URL: ${{ secrets.NEON_DATABASE_URL }}",
        "EXPECTED_DATABASE_NAME: ${{ vars.NEON_DATABASE_NAME }}",
        "EXPECTED_DATABASE_SCHEMA: ${{ vars.NEON_DATABASE_SCHEMA }}",
        "EXPECTED_DATABASE_HOST: ${{ vars.NEON_DATABASE_HOST }}",
    ):
        assert setting in workflow
    assert "RENDER_DATABASE_" not in workflow
    assert "NEON_DATABASE_CA" not in workflow
    assert "cleanup-ca" not in workflow
    command_step = migrate_job.split("- name: Apply the scoped customer email migration", 1)[1].split("- name:", 1)[0]
    assert migration_command in command_step
    assert '--confirmation "$MIGRATION_CONFIRMATION"' in command_step
    assert '--backup-confirmation "$BACKUP_CONFIRMATION"' in command_step
    assert ">/dev/null" not in command_step
    assert "2>&1" not in command_step
    assert "post-migration schema validation" in migrate_job
    assert command_step.index(migration_command) < migrate_job.index("post-migration schema validation")

    # Model the job dependency/gates: only a successful preflight plus a migrate
    # operation on the fixed ref can make the migration job eligible.
    def migration_job_eligible(operation, preflight_succeeded, ref):
        return (preflight_succeeded and ref == "refs/heads/main"
                and operation == "migrate")
    assert not migration_job_eligible("preflight", True, "refs/heads/main")
    assert not migration_job_eligible("preflight", False, "refs/heads/main")
    assert not migration_job_eligible("migrate", False, "refs/heads/main")
    assert not migration_job_eligible("migrate", True, "refs/heads/responsive-all-pages")
    assert migration_job_eligible("migrate", True, "refs/heads/main")


@pytest.mark.parametrize("preflight_succeeds", [True, False])
def test_preflight_only_never_has_an_executable_migration_path(preflight_succeeds):
    workflow = Path(".github/workflows/migrate-customer-email-schema.yml").read_text()
    jobs = re.split(r"(?m)^  (?:preflight|migrate):\s*$", workflow)
    preflight_job, migrate_job = jobs[1], jobs[2]
    migration_command = "python -m flask --app manage:app migrate-customer-email-schema"
    assert migration_command not in preflight_job
    gate_match = re.search(r"(?m)^    if:\s*\$\{\{\s*(.*?)\s*\}\}\s*$", migrate_job)
    assert gate_match
    gate_terms = {term.strip() for term in gate_match.group(1).split("&&")}
    assert gate_terms == {
        "needs.preflight.result == 'success'",
        "github.ref == 'refs/heads/main'",
        "inputs.operation == 'migrate'",
    }
    assert "needs: preflight" in migrate_job
    # Evaluate the actual three-term gate for the preflight-only choice both
    # when its read-only check passes and when it fails.
    results = {
        "needs.preflight.result == 'success'": preflight_succeeds,
        "github.ref == 'refs/heads/main'": True,
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


def test_engine_url_overrides_weaker_tls_query_settings(tmp_path, monkeypatch):
    ca_file = tmp_path / "system-ca.pem"
    ca_file.write_text("fixture", encoding="ascii")
    monkeypatch.setattr("scripts.customer_email_schema_guard.trusted_system_ca_bundle", lambda: ca_file)
    url = _engine_url(_database_settings()["DATABASE_URL"])
    query = make_url(url).query
    assert query["sslmode"] == "verify-full"
    assert query["sslrootcert"] == str(ca_file)


def _write_valid_ca_bundle(path):
    certificates = ssl.create_default_context().get_ca_certs(binary_form=True)
    assert certificates
    path.write_text(
        "".join(ssl.DER_cert_to_PEM_cert(certificate) for certificate in certificates),
        encoding="ascii",
    )


def test_system_ca_bundle_must_exist_and_load(monkeypatch, tmp_path):
    from scripts.customer_email_schema_guard import trusted_system_ca_bundle

    missing = tmp_path / "missing-ca.pem"
    monkeypatch.setattr("scripts.customer_email_schema_guard.SYSTEM_CA_BUNDLE", missing)
    with pytest.raises(GuardError, match="system PostgreSQL CA bundle is missing"):
        trusted_system_ca_bundle()

    invalid = tmp_path / "invalid-ca.pem"
    invalid.write_text("not a certificate bundle", encoding="ascii")
    monkeypatch.setattr("scripts.customer_email_schema_guard.SYSTEM_CA_BUNDLE", invalid)
    with pytest.raises(GuardError, match="system PostgreSQL CA bundle could not be loaded"):
        trusted_system_ca_bundle()


def test_tls_environment_generation_uses_validated_system_ca(tmp_path, monkeypatch, capsys):
    settings = _database_settings()
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    ca_file = tmp_path / "ca-certificates.crt"
    _write_valid_ca_bundle(ca_file)
    monkeypatch.setattr("scripts.customer_email_schema_guard.SYSTEM_CA_BUNDLE", ca_file)
    github_env = tmp_path / "github-env"
    github_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_ENV", str(github_env))

    export_verified_migration_environment()

    contents = github_env.read_text(encoding="utf-8")
    assert "sslmode=verify-full" in contents
    generated_url = contents.split("DATABASE_URL=", 1)[1].splitlines()[0]
    assert generated_url.startswith("postgresql://")
    assert make_url(generated_url).query["sslrootcert"] == str(ca_file)
    next_step_settings = {**settings, "DATABASE_URL": generated_url}
    assert required_settings(next_step_settings) == (
        generated_url, "store_db", "public", "db.example.invalid"
    )
    assert make_url(_engine_url(generated_url)).drivername == "postgresql+psycopg"
    assert f"DATABASE_CA_CERT_FILE={ca_file}" in contents
    assert "PGSSLMODE=verify-full" in contents
    assert f"PGSSLROOTCERT={ca_file}" in contents
    assert "-----BEGIN CERTIFICATE-----" not in contents
    assert "unit-test-password" not in capsys.readouterr().out


def test_read_only_connect_uses_system_ca_and_verify_full(tmp_path, monkeypatch):
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
    ca_file = tmp_path / "system-ca.pem"
    ca_file.write_text("fixture", encoding="ascii")
    monkeypatch.setattr("scripts.customer_email_schema_guard.trusted_system_ca_bundle", lambda: ca_file)
    monkeypatch.setattr("scripts.customer_email_schema_guard.required_settings", lambda: (
        _database_settings()["DATABASE_URL"], "store_db", "public", "db.example.invalid"
    ))

    connection = _connect()
    assert calls["connect_args"] == {
        "sslmode": "verify-full", "sslrootcert": str(ca_file),
        "options": "-c default_transaction_read_only=on",
    }
    query = make_url(calls["url"]).query
    assert query["sslmode"] == "verify-full"
    assert query["sslrootcert"] == str(ca_file)


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
    ("unable to get local issuer certificate for private-host", "CA trust/certificate-chain verification failure"),
    ("certificate verify failed for private-host", "CA trust/certificate-chain verification failure"),
    ("certificate subject name for private-host does not match host name", "hostname certificate verification failure"),
    ("could not read root certificate file CA_SECRET.pem", "invalid TLS configuration"),
    ("TLS handshake failure with private-host", "other TLS handshake failure"),
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


def test_specific_cause_wins_over_wrapped_generic_tls_error():
    cause = RuntimeError(
        "hostname mismatch for database private-host, IP 192.0.2.8; "
        "URL postgresql://user:password@private-host/private-db"
    )
    wrapper = RuntimeError("SSL error while opening connection")
    wrapper.__cause__ = cause

    assert _connection_failure_category(wrapper) == "hostname certificate verification failure"


def test_certificate_chain_cause_is_found_without_exposing_it_in_summary(monkeypatch, tmp_path):
    secret_detail = (
        "unable to get local issuer certificate for private-host; "
        "IP 192.0.2.9; postgresql://user:password@private-host/private-db"
    )
    wrapper = RuntimeError("SQLAlchemy connection error")
    wrapper.__cause__ = RuntimeError(secret_detail)
    monkeypatch.setattr(
        "scripts.customer_email_schema_guard._safe_driver_versions",
        lambda: "psycopg=3.3.6 implementation=binary libpq=180006",
    )
    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

    diagnostic = safe_failure("Database connection/identity check", wrapper)
    write_summary([str(diagnostic)])
    output = str(diagnostic) + summary_path.read_text(encoding="utf-8")

    assert "category=CA trust/certificate-chain verification failure" in output
    assert "psycopg=3.3.6 implementation=binary libpq=180006" in output
    for secret in ("private-host", "192.0.2.9", "user:password", "private-db", secret_detail):
        assert secret not in output


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


def test_existing_customer_email_schema_without_new_studio_tables_is_upgradeable():
    assert classify_tables(PRE_EMAIL_STUDIO_TABLES) == "upgradable"
    assert classify_tables(PRE_EMAIL_STUDIO_TABLES | (EMAIL_STUDIO_TABLES - {"email_draft"})) == "upgradable"


def test_preflight_accepts_prior_schema_and_identifies_missing_studio_migration(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'prior-email-schema.db'}")
    legacy_tables = [table for table in db.metadata.sorted_tables if table.name not in APPROVED_MISSING_TABLES]
    db.metadata.create_all(engine, tables=legacy_tables)
    try:
        with engine.connect() as connection:
            notes = validate_schema(connection, "public", allow_approved_missing=True)
            assert any("approved missing table: email_template" in note for note in notes)
            assert any("approved missing table: email_draft" in note for note in notes)
            with pytest.raises(GuardError, match="Unexpected missing table"):
                validate_schema(connection, "public", allow_approved_missing=False)
    finally:
        engine.dispose()


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
        "unique": False, "valid": True, "has_predicate": False, "predicate": None,
        "included_columns": (), "access_method": "btree",
        "key_count": 1, "total_count": 1,
        "key_order": ("ASC NULLS LAST",), "collations_match": (True,),
        "operator_classes_default": (True,), "ready": True, "live": True,
    }
    snapshot.update(changes)
    return snapshot


def test_postgres_index_catalog_snapshot_accepts_expected_plain_btree():
    validate_postgres_index_snapshot(
        "ix_sample", "sample", ("account_id",), False, _index_snapshot(),
        strict_definition=True,
    )


def test_postgres_index_catalog_snapshot_accepts_expected_partial_btree():
    snapshot = _index_snapshot(
        name="uq_email_investigation_open_message",
        table="email_delivery_investigation",
        columns=("message_id",),
        unique=True,
        has_predicate=True,
        predicate="(status)::text <> 'RESOLVED'::text",
    )
    validate_postgres_index_snapshot(
        "uq_email_investigation_open_message",
        "email_delivery_investigation",
        ("message_id",),
        True,
        snapshot,
        expected_predicate="status <> 'RESOLVED'",
        strict_definition=True,
    )


@pytest.mark.parametrize(("has_predicate", "predicate"), [
    (False, None),
    (True, "status = 'RESOLVED'"),
])
def test_postgres_index_catalog_snapshot_rejects_wrong_partial_predicate(has_predicate, predicate):
    with pytest.raises(GuardError, match="Incompatible PostgreSQL index"):
        validate_postgres_index_snapshot(
            "uq_email_investigation_open_message",
            "email_delivery_investigation",
            ("message_id",),
            True,
            _index_snapshot(
                name="uq_email_investigation_open_message",
                table="email_delivery_investigation",
                unique=True,
                has_predicate=has_predicate,
                predicate=predicate,
            ),
            expected_predicate="status <> 'RESOLVED'",
            strict_definition=True,
        )


@pytest.mark.parametrize("changes", [
    {"has_predicate": True},
    {"included_columns": ("created_at",), "total_count": 2},
    {"access_method": "hash"},
    {"columns": ("created_at",)},
    {"unique": True},
    {"valid": False},
    {"table": "other_table"},
    {"key_order": ("DESC NULLS FIRST",)},
    {"collations_match": (False,)},
    {"operator_classes_default": (False,)},
    {"ready": False},
    {"live": False},
])
def test_postgres_index_catalog_snapshot_rejects_incompatible_indexes(changes):
    with pytest.raises(GuardError, match="Incompatible PostgreSQL index"):
        validate_postgres_index_snapshot(
            "ix_sample", "sample", ("account_id",), False, _index_snapshot(**changes),
            strict_definition=True,
        )


def test_postgres_index_catalog_snapshot_fails_closed_on_missing_metadata():
    incomplete = _index_snapshot()
    del incomplete["operator_classes_default"]
    with pytest.raises(GuardError, match="metadata incomplete"):
        validate_postgres_index_snapshot(
            "ix_sample", "sample", ("account_id",), False, incomplete,
            strict_definition=True,
        )


def test_non_strict_index_preflight_ignores_historical_catalog_details():
    altered = _index_snapshot(
        key_order=("DESC NULLS FIRST",),
        collations_match=(False,),
        operator_classes_default=(False,),
    )
    validate_postgres_index_snapshot(
        "ix_sample", "sample", ("account_id",), False, altered
    )


class _CatalogResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self.rows


class _MockPostgresCatalogConnection:
    class dialect:
        name = "postgresql"

    def __init__(self, rows):
        self.rows = rows
        self.statement = ""
        self.params = None

    def execute(self, statement, params):
        self.statement = str(statement)
        self.params = params
        return _CatalogResult(self.rows)


def test_postgres_index_catalog_reads_order_collation_and_operator_class_from_mocked_catalog():
    connection = _MockPostgresCatalogConnection([{
        "index_name": "ix_sample", "table_name": "sample", "is_unique": False,
        "is_valid": True, "is_ready": True, "is_live": True,
        "has_predicate": False, "key_count": 1, "total_count": 1,
        "predicate": None,
        "access_method": "btree", "key_columns": ["account_id"],
        "included_columns": [], "key_order": ["ASC NULLS LAST"],
        "collations_match": [True], "operator_classes_default": [True],
    }])

    catalog = postgres_index_catalog(
        connection, "public", "sample", {"ix_sample"}, strict_definition=True
    )["ix_sample"]

    assert catalog == _index_snapshot()
    assert "indoption" in connection.statement
    assert "indcollation" in connection.statement
    assert "pg_opclass" in connection.statement


def _inspected_foreign_key(**changes):
    foreign_key = {
        "name": "fk_sample_account",
        "constrained_columns": ["account_id"],
        "referred_schema": "public",
        "referred_table": "customer_account",
        "referred_columns": ["id"],
        "options": {},
    }
    foreign_key.update(changes)
    return foreign_key


def _foreign_key_catalog_snapshot(**changes):
    snapshot = {
        "name": "fk_sample_account",
        "table": "sample",
        "referred_schema": "public",
        "referred_table": "customer_account",
        "validated": True,
        "deferrable": False,
        "initially_deferred": False,
        "match_type": "SIMPLE",
        "on_update": "NO ACTION",
        "on_delete": "NO ACTION",
        "columns": ("account_id",),
        "referred_columns": ("id",),
    }
    snapshot.update(changes)
    return snapshot


def test_postgres_foreign_key_catalog_accepts_matching_validated_mocked_constraint():
    validate_postgres_foreign_key_catalog(
        [_inspected_foreign_key()], [_foreign_key_catalog_snapshot()], "public", "sample"
    )


@pytest.mark.parametrize("changes", [
    {"validated": False},
    {"columns": ("purpose",)},
    {"referred_schema": "other"},
    {"referred_table": "other_table"},
    {"referred_columns": ("email",)},
    {"match_type": "FULL"},
    {"on_update": "CASCADE"},
    {"on_delete": "CASCADE"},
    {"deferrable": True},
    {"initially_deferred": True},
])
def test_postgres_foreign_key_catalog_rejects_incompatible_mocked_constraints(changes):
    with pytest.raises(GuardError):
        validate_postgres_foreign_key_catalog(
            [_inspected_foreign_key()],
            [_foreign_key_catalog_snapshot(**changes)],
            "public", "sample",
        )


def test_postgres_foreign_key_catalog_reads_validation_and_options_from_mocked_catalog():
    connection = _MockPostgresCatalogConnection([{
        "constraint_name": "fk_sample_account",
        "table_name": "sample",
        "referred_schema": "public",
        "referred_table": "customer_account",
        "validated": True,
        "deferrable": False,
        "initially_deferred": False,
        "match_type": "SIMPLE",
        "on_update": "NO ACTION",
        "on_delete": "NO ACTION",
        "columns": ["account_id"],
        "referred_columns": ["id"],
    }])

    catalog = postgres_foreign_key_catalog(connection, "public", "sample")

    assert catalog == [_foreign_key_catalog_snapshot()]
    assert "convalidated" in connection.statement
    assert "confmatchtype" in connection.statement
    assert "confupdtype" in connection.statement
    assert "confdeltype" in connection.statement


def test_unrelated_additional_indexes_are_allowed_by_policy():
    # The guard validates every migration-declared name but tolerates unrelated
    # local indexes; this avoids rejecting safe operator-created performance indexes.
    assert validate_missing_state(
        "sample", {"ix_expected"}, {"ix_expected", "ix_operator_added"}, set(), False, "indexes"
    ) == set()


def test_manifest_reads_the_actual_sql_indexes_and_defaults():
    indexes = migration_index_manifest()
    defaults = migration_column_defaults()
    columns = migration_column_manifest()
    unique, named_unique, foreign_keys = migration_constraint_manifest()
    assert indexes["ix_marketing_campaign_run_campaign_key"] == (
        "marketing_campaign_run", ("campaign_key",), True, None
    )
    assert indexes["uq_email_investigation_open_message"] == (
        "email_delivery_investigation", ("message_id",), True, "status <> 'RESOLVED'"
    )
    assert defaults[("customer_account", "two_factor_enabled")] == "FALSE"
    assert defaults[("email_message", "retry_count")] == "0"
    assert columns[("customer_consent_event", "wording_version")].sql_type == "VARCHAR(120)"
    assert unique["customer_order_link"] == {("order_id",)}
    assert named_unique["email_campaign_recipient"]["uq_campaign_recipient_account"] == ("campaign_id", "account_id")
    assert ("account_id", "customer_account", "id") in foreign_keys["email_message"]


def test_complete_local_schema_passes_strict_postcheck():
    engine = create_engine("sqlite://")
    db.metadata.create_all(engine)
    try:
        with engine.connect() as connection:
            from app.services.email_outbox_schema import apply_email_outbox_migration
            from app.services.email_delivery_ops import apply_email_delivery_ops_migration
            from app.services.email_legacy_reviews import apply_email_legacy_review_migration
            from app.services.email_test_purpose_migration import apply_email_test_purpose_migration
            from app.services.consent_audit import apply_consent_audit_migration
            with connection.begin():
                apply_email_outbox_migration(connection, "sqlite")
                apply_email_test_purpose_migration(connection, "sqlite")
                apply_email_delivery_ops_migration(connection, "sqlite")
                apply_email_legacy_review_migration(connection, "sqlite")
                apply_consent_audit_migration(connection, "sqlite")
            legacy_columns = {
                column["name"]: column
                for column in inspect(connection).get_columns("email_legacy_payload_review_event")
            }
            assert legacy_columns["action"]["default"] == "'quarantined'"
            assert legacy_columns["provider_outcome"]["default"] == "'unknown'"
            assert legacy_columns["provider_evidence"]["default"] == "''"
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


def _create_sqlite_migration_schema(engine, tables):
    from sqlalchemy import text
    from sqlalchemy.schema import DefaultClause

    manifest = migration_column_manifest()
    original_defaults = []
    for table in tables:
        for column in table.columns:
            spec = manifest.get((table.name, column.name))
            if spec:
                original_defaults.append((column, column.server_default, column.nullable))
                column.nullable = spec.nullable
                if spec.default not in {None, "__serial__"}:
                    column.server_default = DefaultClause(text(spec.default))
    try:
        db.metadata.create_all(engine, tables=tables, checkfirst=True)
    finally:
        for column, server_default, nullable in original_defaults:
            column.server_default = server_default
            column.nullable = nullable


def _historical_schema_engine():
    engine = create_engine("sqlite://")
    _create_sqlite_migration_schema(engine, db.metadata.sorted_tables)
    with engine.begin() as connection:
        for table in (
            "email_delivery_investigation_event",
            "email_delivery_investigation",
            "email_delivery_attempt",
            "email_worker_run",
            "email_legacy_payload_review_event",
        ):
            connection.exec_driver_sql(f"DROP TABLE {table}")
    return engine


def test_strict_historical_schema_accepts_complete_historical_state():
    engine = _historical_schema_engine()
    try:
        with engine.connect() as connection:
            notes = validate_historical_schema(connection)
        assert any("Complete historical customer/email schema validated" in note for note in notes)
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", sorted(HISTORICAL_REQUIRED_TABLES))
def test_strict_historical_schema_rejects_each_missing_historical_table(table):
    engine = _historical_schema_engine()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"DROP TABLE {table}")
        with engine.connect() as connection, pytest.raises(
            GuardError, match=f"required tables are missing: {table}"
        ):
            validate_historical_schema(connection)
    finally:
        engine.dispose()


@pytest.mark.parametrize("table,column", sorted({
    ("customer_account", "two_factor_enabled"),
    ("email_message", "retry_count"),
    ("email_message", "provider_message_id"),
    ("email_template", "name"),
    ("email_template", "blocks_json"),
    ("email_draft", "blocks_json"),
    ("email_message", "template_id"),
}))
def test_strict_historical_schema_rejects_each_missing_historical_column(table, column):
    engine = _historical_schema_engine()
    try:
        with engine.begin() as connection:
            if (table, column) == ("email_message", "provider_message_id"):
                connection.exec_driver_sql("DROP INDEX ix_email_message_provider_message_id")
            elif (table, column) == ("email_message", "template_id"):
                connection.exec_driver_sql("DROP INDEX ix_email_message_template_id")
            connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")
        with engine.connect() as connection, pytest.raises(
            GuardError, match=f"approved missing column: {table}.{column}"
        ):
            validate_historical_schema(connection)
    finally:
        engine.dispose()


@pytest.mark.parametrize("index", sorted({
    "ix_email_message_provider_message_id",
    "ix_email_message_template_id",
}))
def test_strict_historical_schema_rejects_each_missing_historical_index(index):
    engine = _historical_schema_engine()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"DROP INDEX {index}")
        with engine.connect() as connection, pytest.raises(
            GuardError, match=f"approved missing index: {index}"
        ):
            validate_historical_schema(connection)
    finally:
        engine.dispose()


def test_strict_historical_schema_rejects_removed_campaign_recipient_unique_constraint():
    engine = _historical_schema_engine()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE email_campaign_recipient")
            connection.exec_driver_sql("""
                CREATE TABLE email_campaign_recipient (
                    id INTEGER PRIMARY KEY,
                    campaign_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    recipient VARCHAR(254) NOT NULL,
                    status VARCHAR(24) NOT NULL DEFAULT 'queued',
                    failure_reason VARCHAR(255) NOT NULL DEFAULT '',
                    sent_at DATETIME
                )
            """)
            connection.exec_driver_sql(
                "CREATE INDEX ix_email_campaign_recipient_campaign_id "
                "ON email_campaign_recipient (campaign_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_email_campaign_recipient_account_id "
                "ON email_campaign_recipient (account_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_email_campaign_recipient_status "
                "ON email_campaign_recipient (status)"
            )
        with engine.connect() as connection, pytest.raises(
            GuardError, match="Missing or incompatible unique constraint on email_campaign_recipient"
        ):
            validate_historical_schema(connection)
    finally:
        engine.dispose()


def test_strict_historical_schema_rejects_missing_historical_foreign_key():
    engine = _historical_schema_engine()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE customer_token")
            connection.exec_driver_sql("""
                CREATE TABLE customer_token (
                    id INTEGER PRIMARY KEY,
                    account_id INTEGER NOT NULL,
                    purpose VARCHAR(24) NOT NULL,
                    token_hash VARCHAR(64) NOT NULL,
                    expires_at DATETIME NOT NULL,
                    used_at DATETIME,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_customer_token_token_hash UNIQUE (token_hash)
                )
            """)
            for name, column in (
                ("ix_customer_token_account_id", "account_id"),
                ("ix_customer_token_purpose", "purpose"),
                ("ix_customer_token_token_hash", "token_hash"),
                ("ix_customer_token_expires_at", "expires_at"),
            ):
                connection.exec_driver_sql(f"CREATE INDEX {name} ON customer_token ({column})")
        with engine.connect() as connection, pytest.raises(
            GuardError, match="Missing or incompatible foreign key on customer_token"
        ):
            validate_historical_schema(connection)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "column_type,window_nullability,attempts_default,match",
    [
        ("INTEGER", "NOT NULL", "0", "test-database type"),
        ("VARCHAR(64)", "", "0", "test-database nullability"),
        ("VARCHAR(64)", "NOT NULL", "1", "test-database default"),
    ],
)
def test_strict_historical_schema_rejects_incompatible_sqlite_column_definitions(
    column_type, window_nullability, attempts_default, match
):
    engine = _historical_schema_engine()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE auth_rate_limit")
            connection.exec_driver_sql(
                "CREATE TABLE auth_rate_limit ("
                f"key {column_type} NOT NULL PRIMARY KEY, "
                f"window_started_at DATETIME {window_nullability}, "
                f"attempts INTEGER NOT NULL DEFAULT {attempts_default})"
            )
        with engine.connect() as connection, pytest.raises(GuardError, match=match):
            validate_historical_schema(connection)
    finally:
        engine.dispose()


def test_strict_historical_schema_rejects_email_template_category_uniqueness():
    engine = _historical_schema_engine()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_email_template_category_test ON email_template (category)"
            )
        with engine.connect() as connection, pytest.raises(
            GuardError, match="Missing or incompatible unique constraint on email_template"
        ):
            validate_historical_schema(connection)
    finally:
        engine.dispose()
